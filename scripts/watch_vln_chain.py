#!/usr/bin/env python3
"""Live view of the whole VLN command chain, for acceptance runs.

Run INSIDE the container:

    source /opt/ros/humble/setup.bash
    export RMW_IMPLEMENTATION=rmw_zenoh_cpp
    python3 scripts/watch_vln_chain.py 2>/dev/null

One rclpy node subscribes to every hop plus the node's own status topic and
prints a refreshed table once a second.  (Shelling out to `ros2 topic hz` per
topic takes ~15 s per topic, which is useless for watching a moving robot.)  The
zenoh router logs "Received Data for unknown expr_id" noise to stderr, hence the
`2>/dev/null`.

The table is what tells apart the failure modes that look identical from the
outside:

  * a hop with 0 publishers        -> the stage above it is not running
  * a rate far below 50 Hz         -> that stage is stalling
  * raw topic silent but the model
    state says "running"           -> the server is answering with nothing usable
  * consecutive failures climbing  -> the remote model is unreachable; the node
                                      zeroes the command on the third one
  * vx pinned at 0 while wz moves  -> the chassis guard is refusing translation,
                                      which on the Ranger means the turn-radius
                                      clamp is holding the command at the radius

`vx`/`wz` are the last values seen on that hop, not an average: the point is to
see the command change as the model re-plans, not to smooth it.

Ctrl-C to exit.
"""

from __future__ import annotations

import json
import sys
import time

import rclpy
from geometry_msgs.msg import Twist
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import String


STATUS_TOPIC = "/ranger/vln/status"

# hop topic -> (what it means, the stage that must own it)
CHAIN: tuple[tuple[str, str, str], ...] = (
    ("/ranger/vln/cmd_vel_raw", "模型输出：discrete_action 或 trajectory 换算成的 Twist", "vln node"),
    ("/cmd_vel_nav", "source mux 唯一输出（navigation 与 vln 二选一）", "source mux"),
    ("/cmd_vel", "chassis guard 输出 -> 底盘（只允许这一个发布者）", "chassis guard"),
)

SOLE_OWNER = {
    "/cmd_vel": "chassis guard",
    "/cmd_vel_nav": "source mux",
    "/ranger/vln/cmd_vel_raw": "vln node",
}


class _Hop:
    __slots__ = ("topic", "label", "owner", "count", "vx", "wz", "last")

    def __init__(self, topic: str, label: str, owner: str) -> None:
        self.topic = topic
        self.label = label
        self.owner = owner
        self.count = 0
        self.vx = 0.0
        self.wz = 0.0
        self.last: float | None = None


class ChainWatcher(Node):
    def __init__(self) -> None:
        super().__init__("vln_chain_watcher")
        self._hops = [_Hop(topic, label, owner) for topic, label, owner in CHAIN]
        self._last_counts: dict[int, int] = {id(hop): 0 for hop in self._hops}
        self._status: dict = {}
        self._status_at: float | None = None
        self._status_count = 0
        for hop in self._hops:
            self.create_subscription(Twist, hop.topic, self._make_callback(hop), 10)
        self.create_subscription(String, STATUS_TOPIC, self._on_status, 10)
        self.create_timer(1.0, self._render)

    def _make_callback(self, hop: _Hop):
        def _on(message: Twist) -> None:
            hop.count += 1
            hop.vx = float(message.linear.x)
            hop.wz = float(message.angular.z)
            hop.last = time.monotonic()

        return _on

    def _on_status(self, message: String) -> None:
        self._status_count += 1
        self._status_at = time.monotonic()
        try:
            payload = json.loads(message.data)
        except ValueError:
            self._status = {"unparseable": message.data[:120]}
            return
        self._status = payload if isinstance(payload, dict) else {}

    def _render(self) -> None:
        now = time.monotonic()
        # In dry-run the node deliberately creates no velocity publisher and the
        # chassis guard is never started, so `pub 0` on every hop is the DESIGNED
        # state, not a node that failed to come up.  The status topic at the
        # bottom is what proves the node is alive; never read `pub 0` alone as
        # "not running" without looking at mode first.
        mode = str(self._status.get("mode", "?"))
        dry_run = mode == "dry-run"
        lines = [
            f"VLN command chain   {time.strftime('%H:%M:%S')}   mode={mode}   (Ctrl-C 退出)",
            "",
            f"{'topic':<28}{'pub':>4}{'sub':>4}{'Δmsg':>6}{'vx':>10}{'wz':>10}  note",
            "-" * 96,
        ]
        for hop in self._hops:
            pubs = len(self.get_publishers_info_by_topic(hop.topic))
            subs = len(self.get_subscriptions_info_by_topic(hop.topic))
            stale = hop.last is None or (now - hop.last) > 2.0
            delta = hop.count - self._last_counts[id(hop)]
            self._last_counts[id(hop)] = hop.count

            note = ""
            if pubs == 0 and dry_run:
                note = "dry-run: no velocity publisher by design"
            elif pubs == 0:
                note = f"no publisher <- {hop.owner} is not running"
            elif pubs > 1:
                note = f"!! {pubs} publishers on a single-owner topic"
            elif stale:
                note = "no data"
            elif abs(hop.vx) < 1e-9 and abs(hop.wz) > 1e-9 and delta > 0:
                note = "pivot in place (vx=0)"
            elif abs(hop.vx) < 1e-9 and abs(hop.wz) < 1e-9 and delta > 0:
                note = "zero (holding)"

            vx = "-" if stale else f"{hop.vx:+.3f}"
            wz = "-" if stale else f"{hop.wz:+.3f}"
            lines.append(
                f"{hop.topic:<28}{pubs:>4}{subs:>4}{delta:>6}{vx:>10}{wz:>10}  {note}"
            )
            lines.append(f"    {hop.label}")
        lines.append("")

        lines.extend(self._status_lines(now))
        lines.append("")
        if dry_run:
            lines.append("CHECK  mode=dry-run：三个速度话题的 pub 都应恰好为 0，车不会动")
            lines.append("CHECK  判活的依据是上面那行 status：age 小、frames 在涨、healthy=True")
            lines.append("CHECK  每个 episode 的最后一帧都会写 last_note，看它变了就说明模型在回话")
            lines.append("NOTE   sub 计数包含本观测器自己，所以比实际多 1")
            lines.append("NOTE   切到 live 后判据才反过来：pub=1、Δmsg 在涨，见 docs/ACCEPTANCE.md")
        else:
            lines.append("CHECK  /cmd_vel 的 pub 必须恰好为 1（chassis guard 独占）")
            lines.append("CHECK  /cmd_vel_nav 的 pub 必须恰好为 1（source mux 独占）")
            lines.append("CHECK  /ranger/vln/cmd_vel_raw 必须持续有 Δmsg；为 0 说明模型没在回话")
            lines.append(
                "CHECK  odometry 必须是 fresh；servo 模式下没 pose 就停着不动（servo_wait_odom）"
            )
            lines.append(
                "CHECK  servo 应在 chasing 与 goal reached 之间来回切；一直 idle 说明模型没给可执行的动作"
            )
            lines.append("NOTE   sub 计数包含本观测器自己，所以比实际多 1")
        print("\033[2J\033[H" + "\n".join(lines), flush=True)

    def _status_lines(self, now: float) -> list[str]:
        if self._status_at is None:
            return [
                f"{STATUS_TOPIC}  (node status)",
                "    no status yet -- the vln node is not running, or it was "
                "started after this watcher",
            ]
        age = now - self._status_at
        payload = self._status
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
        line = (
            f"    age {age:4.1f}s  mode={payload.get('mode', '?')}  "
            f"source={payload.get('source', '?')}  plan={plan.get('reason', '?')}  "
            f"state={client.get('state', '?')}  episode={client.get('episode', '?')}  "
            f"frames={client.get('requests', '?')}  flushes={client.get('memory_flushes', 0)}  "
            f"failures={client.get('consecutive_failures', '?')}  "
            f"healthy={client.get('healthy', '?')}"
        )
        # What the last plan was: "discrete_action[2]..." means the model asked
        # for the turn, "... pivoting" means this stack judged the path tighter
        # than the chassis can steer, and "memory flushed" marks a history reset.
        if client.get("last_note"):
            line += f"  note={client['last_note']}"
        lines = [f"{STATUS_TOPIC}  (node status, {self._status_count} msgs)", line]
        step = payload.get("step") if isinstance(payload.get("step"), dict) else {}
        if step:
            # Stop-and-go: "looking" is the inference gate.  While it is False
            # the robot is executing a step or settling, and no frame is being
            # captured -- a stale `plan=active_plan` there is expected.
            # `feedback` says what will END the segment the robot is driving:
            # "odom" (measured: it stops when the angle/distance is achieved)
            # or "timer" (the old nominal-duration path -- no odometry, or a
            # segment with no goal such as STOP or a followed trajectory).
            lines.append(
                f"    lockstep={step.get('lockstep', '?')}  "
                f"step={step.get('reason', '?')}  "
                f"segment={step.get('segment', 0)}/{step.get('segments', 0)}  "
                f"goal={step.get('goal') or '-'}  "
                f"feedback={step.get('feedback', '?')}  "
                f"looking={'yes' if step.get('looking') else 'no'}  "
                f"(nominal {step.get('action_step_s', '?')}s + "
                f"{step.get('settle_s', '?')}s settle)"
            )
        servo = payload.get("servo") if isinstance(payload.get("servo"), dict) else {}
        odom = payload.get("odom") if isinstance(payload.get("odom"), dict) else {}
        if odom:
            # What a missing pose *means* depends on who needs it: the servo
            # refuses to drive a goal it cannot measure (it holds), while the
            # segment stepper falls back to the nominal duration.
            if not odom.get("subscribed"):
                state = "OFF (nothing subscribes; steps are timing-only)"
            elif not odom.get("received"):
                state = f"NOT RECEIVING on {odom.get('topic', '?')}"
            elif not odom.get("fresh"):
                state = "stale"
            else:
                state = "fresh"
            if state != "fresh" and odom.get("subscribed"):
                state += (
                    " -- the servo holds (servo_wait_odom), nothing is driven blind"
                    if servo.get("enabled")
                    else " -- steps fall back to the timer"
                )
            lines.append(f"    odometry: {state}")
        if servo:
            if not servo.get("enabled"):
                state = "OFF (segment execution: see lockstep/step above)"
            elif not servo.get("active"):
                state = "idle (no goal: no plan, a STOP, or no pose on the frame)"
            elif servo.get("done"):
                state = "goal reached -- holding for the next answer"
            else:
                goal = servo.get("goal") or {}
                where = (
                    f"({goal.get('x')}, {goal.get('y')}, {goal.get('yaw_deg')}deg)"
                    if goal
                    else "-"
                )
                state = (
                    f"chasing {where}  err={servo.get('translation_error_m')}m "
                    f"dyaw={servo.get('yaw_error_deg')}deg"
                )
            lines.append(f"    servo: {state}")
        lines.append(
            f"    instruction pending={client.get('instruction') or '-'}  "
            f"active={client.get('active_instruction') or '-'}  "
            f"reset_pending={client.get('reset_pending', '?')}"
        )
        lines.append(
            f"    executed vx={client.get('executed_vx', '?')} "
            f"wz={client.get('executed_wz', '?')}  (what the node thinks it sent)"
        )
        safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
        if safety:
            nearest = safety.get("nearest_m")
            gaps = (
                f"nearest={float(nearest):.2f}m"
                if isinstance(nearest, (int, float))
                else "nearest=n/a"
            )
            lines.append(
                f"    proximity gate: "
                f"{'BLOCKED' if safety.get('blocked') else 'clear'}  "
                f"reason={safety.get('reason', '?')}  {gaps}  "
                f"valid_px={safety.get('valid_pixels', '?')}"
            )
        operator_payload = payload.get("operator")
        operator = operator_payload if isinstance(operator_payload, dict) else {}
        task_safety = (
            operator.get("safety") if isinstance(operator.get("safety"), dict) else {}
        )
        if task_safety:
            # What the Robonix capability reports: a closed gate is a stopped
            # primitive, not a slow model.  `held` is the instruction that will
            # start by itself once the gate reopens.
            state = (
                "HELD -- execution halted" if task_safety.get("blocked") else "clear"
            )
            lines.append(
                f"    robonix task: {state}  "
                f"safety_stops={task_safety.get('stops', '?')}  "
                f"held={task_safety.get('held_instruction')!r}"
            )
        if age > 5.0:
            lines.append("    STALE: the vln node publishes this at 1 Hz")
        if client.get("last_note"):
            lines.append(f"    last_note: {client['last_note']}")
        if client.get("last_error"):
            lines.append(f"    last_error: {client['last_error']}")
        if client.get("healthy") is False:
            lines.append(
                "    !! the model is unhealthy: the node zeroes the command at "
                "fault_after_failures and the robot stops"
            )
        return lines


def main() -> int:
    rclpy.init(args=sys.argv[1:])
    node = ChainWatcher()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        # ExternalShutdownException is what a SIGTERM (e.g. `timeout`) looks
        # like; exit quietly instead of dumping a traceback over the table.
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
