# InternVLA-N1 DualVLN navigation bridge (Ranger Mini V3)

Takes a natural-language instruction such as *"walk to the door"*, sends the
camera stream to a remote InternVLA-N1 DualVLN server, and turns the model's
answers into velocity commands the Ranger chassis can execute.

The remote model server is not part of this repository. Upstream InternVLA-N1
ships `scripts/realworld/http_internvla_server.py`, and as shipped it exposes
exactly one route: `POST /eval_dual`. This package speaks that one route and
nothing else -- it is not modified, wrapped or proxied. There is no `/health`,
no `/task/start` and no `/task/report`: earlier revisions of this package
invented those three, they exist in neither upstream nor the two local
deployment documents, and they are gone. Check what the compute host actually
answers before arming: `docs/DEPLOY.md` section 4.2.

## Command chain

```
camera ──▶ InternVLANode ──▶ /ranger/vln/cmd_vel_raw
                               │
                               ▼
                        source mux ──▶ /cmd_vel_nav
                               │
                               ▼
                     RangerChassisGuardNode ──▶ /cmd_vel ──▶ ranger_chassis ──▶ CAN
```

`RangerChassisGuardNode` is the only writer of `/cmd_vel`. Nothing in this
package ever talks to `ranger_chassis` directly.

## The four moving parts

| Module | Job |
| --- | --- |
| `http_client.py` | `InternVLAClient` — the one route, `POST /eval_dual` |
| `action_adapter.py` | model output → `VelocityCommand`. The only place that knows what an action *means* |
| `worker.py` | one slow thread doing the HTTP call, decoupled from the 50 Hz dispatcher |
| `ros_node.py` | frame capture, dispatch, mux, chassis guard wiring |

### Why the threading is shaped this way

One DualVLN step takes seconds. The chassis wants a command every 20 ms. Those
two facts cannot share a thread, so:

* the **image callback** only encodes and drops the frame into a one-slot
  mailbox, replacing whatever was there — the worker always resumes on the
  freshest view rather than draining a backlog;
* the **inference worker** blocks inside HTTP and does nothing else;
* the **dispatch timer** never waits for anything. It reads the plan store,
  which returns zero once the plan is older than `plan_hold_s`.

The expiry is the brake. Nothing has to detect a dead model, a dropped link or a
stalled thread: if no fresh plan arrives, the output becomes zero on its own.

### Changing the instruction mid-task

`set_instruction` is called from a ROS callback (or a Robonix RPC thread) and
must never block, so it only records the text and bumps a generation counter.
The blocking `POST /eval_dual` happens on the worker thread at the top of the
next frame, carrying `reset: true` so the server's frame history starts clean.
If a response is already in flight when the instruction changes, its
answer is **discarded** and one zero-velocity step is emitted instead — driving
the robot on a task nobody asked for is worse than pausing for one step.

### Nothing is asked until somebody asks

`require_instruction: true` (the default) means the client boots **unarmed**:
the configured `instruction:` is a reference text for `/ranger/vln/status`, not
a task that arms itself, and until an operator hands one over `evaluate()`
returns a zero plan **without opening a socket**. No request per camera frame,
no plan, no motion.

That gate is the answer to the failure mode that motivated it: DualVLN is
stateless per frame and greedy, so asking it the same question frame after
frame gets the same answer forever. A parked robot with no task is better off
sending nothing at all than asking "is this done yet?" 3–8 s per frame.

An operator **stop** works the same way from the other side: it retires the plan
*and* takes the task away (`clear_instruction`), so the next frame does not
re-ask the model and drive off on its answer. Both doors then wait for the next
instruction. Set `require_instruction: false` for a bench session that should
boot straight into the configured instruction.

## Instruction ingress

Two equivalent paths, both landing in the same runtime slot:

```bash
# ROS, for bench testing
ros2 topic pub --once /ranger/vln/instruction std_msgs/String "{data: 'walk to the door'}"

# Robonix capability
rbnx ask <provider> robonix/primitive/vln/task '{"operation":"set","instruction":"walk to the door"}'
```

Live status is published as JSON on `/ranger/vln/status` at 1 Hz.

## Parameters

`config/internvln_navigation.yaml` holds every setting; `RuntimeConfig` in
`core.py` re-validates all of them at startup. The three that matter most:

* **`server_url`** is the server *base* (`http://host:5801`). Do **not** append
  `/eval_dual` — the client adds the endpoint itself.
* **`require_instruction`** gates the whole chain: with it on (the default) no
  request is sent until an operator issues an instruction, and an operator stop
  returns the stack to that waiting state.
* **`plan_hold_s`** is both the horizon a discrete action is spread over and the
  age at which a plan stops commanding motion. `vx = discrete_forward_m /
  plan_hold_s` and `wz = discrete_step_deg / plan_hold_s`.

`max_plan_age_s` is accepted as an alias for `plan_hold_s` and must be equal to
it; having two names for one horizon is how they drift apart.

## Dry-run

`mode: dry-run` creates **no velocity publisher at all** — see
`test_static_contract.py`. It still subscribes to the camera and the instruction
topic and will call the model as soon as an instruction arrives, so the whole
chain can be watched end to end before the chassis is ever armed. Switch to
`live` only after that.

## Tests

```bash
bash tests/run_offline_tests.sh
```

Everything except `test_ranger_chassis_guard.py` runs without ROS.
