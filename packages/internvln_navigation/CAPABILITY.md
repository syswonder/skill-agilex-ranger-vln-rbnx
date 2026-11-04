---
description: InternVLA-N1 DualVLN language-conditioned navigation with mutually exclusive velocity-source routing on the AgileX Ranger Mini V3.
---

# InternVLA-N1 DualVLN navigation capability

Provider ID: `internvln_navigation`
Namespace: `robonix/primitive/vln`

Initialization accepts the model server, camera, velocity, source-mux and
chassis-guard configuration and validates it through `RuntimeConfig`; an
invalid field fails initialization rather than being silently defaulted.

## Contracts

| Contract | IDL | Purpose |
| --- | --- | --- |
| `robonix/primitive/vln/driver` | `lifecycle/srv/Driver.srv` | Robonix lifecycle: init / activate / deactivate / shutdown |
| `robonix/primitive/vln/task` | `internvln_navigation_control/srv/VlnTask.srv` | `set` / `get` / `stop` the navigation instruction |

`driver` is the lifecycle interface, not a velocity topic. Velocity leaves this
primitive over ROS topics like any other node.

## `vln/task`

```jsonc
// set -- blocks until this task ends
{"operation": "set", "instruction": "go to the chair and stop"}
// -> accepted=true, status="finished" | "stopped" | "running" (timeout),
//    running=false on a terminal state
//    accepted=false or status="held"/"rejected" answers immediately

// get -- never blocks
{"operation": "get"}
// -> running, status, detail   (detail carries armed=, episode= and bound=)
//    plus terminal_kind / terminal_at / terminal_instruction / finished_at

// stop
{"operation": "stop"}
```

Make the instruction a task with an end in it. DualVLN decides "am I there yet"
against the sentence and answers STOP once it judges the goal reached, so a
generic "walk forward along the path" can be called done on nearly any frame.

`set` **waits for the task to end** and `stop` queues. Handing the request to
the ROS side is unchanged — a `POST /eval_dual` round trip takes seconds, so the
handler records the request in a process-global runtime slot and the ROS node
drains it on a 10 Hz timer — but the handler then blocks until the episode
reaches a terminal state (`finished` or `stopped`) or `set_block_timeout_s`
elapses (default 900 s). The Robonix executor has no call timeout, so this is
what makes one plan node live as long as one navigation task instead of
returning in milliseconds and inviting a replan on every tick.

A plan is done when the episode is: after `finished`, a repeated `set` of the
same sentence within `finish_reissue_grace_s` (default 10 s) is answered
`status="finished"` and does not restart the walk; the same sentence while
running answers `already_running` and does not start a second episode. An
operator `stop` is terminal too (`terminal_kind="stopped"`), so stop-then-set of
the same words is a real re-run.

### Idle until asked

With `require_instruction: true` (the default) the stack boots idle: no
instruction in force means the client sends the model **nothing** and the
chassis gets zero velocity. `get` reports `status="awaiting-instruction"` (and
`armed=False` in `detail`) in that state, which is the honest reading — the
stack is not broken, it has nothing to plan about. An operator `stop` returns
it there: the plan is retired *and* the task is dropped, so the next camera
frame does not re-ask the model and drive off on its answer.

## Lifecycle and the shared runtime

`provider.py` owns exactly one ROS 2 context on one thread. On deactivate it
sets the stop event and joins with a 4 s budget; `InternVLANode.close()` zeroes
the plan store and publishes a final zero Twist before the client socket is
closed, so a deactivation cannot leave a live velocity command behind.

The instruction handoff is a module global (`vln_runtime.RUNTIME`) that
`provider.py` and `ros_node.py` both import. The node binds its client and plan
store into it on construction and unbinds on close.

## Command routing

The mux admits two sources, `navigation` and `vln`, exactly one of which is
selected. Only the selected source reaches `/cmd_vel_nav`; a stale or missing
selected source produces zero. `RangerChassisGuardNode` is the sole publisher of
`/cmd_vel` and applies the Ranger's Ackermann and steering-mode constraints.

`mode: dry-run` creates **no velocity publisher at all** — neither in this node
nor in the guard, which is not started. The status topic (`String`) and the
instruction subscription still run, which is what makes dry-run useful.

## Failure behaviour

| Condition | Result |
| --- | --- |
| Model returns a malformed body | that one plan is lost; the store expires normally |
| One request fails | same — one lost plan |
| `fault_after_failures` consecutive failures | `ModelUnavailableError`, and the node **clears the plan store** so a plan from a now-dead model stops being executed |
| Instruction changes mid-flight | in-flight answer discarded, one zero step emitted |
| Never any plan | zero |
| Plan older than `plan_hold_s` | zero |
| Chassis guard receives nothing for `guard_command_timeout_s` | zero |
