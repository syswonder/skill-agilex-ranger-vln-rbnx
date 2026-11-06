# 操作手册 · Ranger InternVLA-N1（保姆级）

> **第一次上车先看 [DEPLOY.md](DEPLOY.md)**：从打包/拷贝到机器人、起容器、build，
> 每个命令的期望输出都在那一页。本页是装好之后"反复开机/测试/排障"用的。

**红线先看**：[SAFETY.md](SAFETY.md)。本手册所有操作**只动 VLN 这一套**
（容器 `robonix_vln_dev`、仓 `skill-agilex-ranger-vln-rbnx`）。
导航（容器 `robonix_ros_dev`、仓 `robot-agilex-ranger_mini_v3`）**只读不改**。

**默认 `mode: dry-run`：机器人不会动。** 要它动，必须切模式并重启
（`scripts/set_vln_mode.sh live --yes`）——见第 6 步。

---

## 第 0 步 · 环境

### 0.1 进目录（在**宿主机**上）

```bash
cd /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx
```

> 如果你 clone 到别的路径，改 `scripts/run_container.sh` 里的 `DEPLOY=`（第 3 步要用）。
> `build.sh` / `start.sh` 自己算路径，不用改。

### 0.2 先输一次 sudo 密码

```bash
sudo -v
```

（脚本要用 docker。只做一次，5 分钟内不用再输。）

### 0.3 准备 .env

```bash
cp .env.example .env
$EDITOR .env
```

**必须填 `INTERNVLA_SERVER_URL`**：算力服务器上 DualVLN 的**根地址**，例如

```
INTERNVLA_SERVER_URL=http://192.168.1.20:5801
```

⚠️ 只写根地址，**不要**带 `/eval_dual`、`/health`、`/task/start`、`/task/report`。
客户端自己会加上它唯一那条 `/eval_dual`。写错了 `start.sh` 会直接报错拦住你。

---

## 第 1 步 · 离线自检（不碰机器人，随时可跑）

```bash
bash scripts/validate_offline.sh
```

它做三件事：跑包内单测、检查 manifest 自洽、检查残留。
最后一行应该是 `offline validation OK`。

**不用起容器、不用连相机、不用连模型服务器、导航栈在跑也没关系。**

只想要 manifest 那部分：

```bash
python3 scripts/validate_manifest_consistency.py
```

它会把命令链、相机话题、guard 上下限、模型地址逐条对一遍，比如：

```
[ OK ] camera wiring: front_camera camera_name -> /camera_435i/color/image_raw -> model
[ OK ] guard limits: guard 0.25 m/s <= chassis ceiling 0.5 m/s (package cap 0.25)
```

---

## 第 2 步 · 先证明模型能连上（这一步别跳）

```bash
python3 scripts/probe_internvla_server.py \
    --url "$(grep -E '^INTERNVLA_SERVER_URL=' .env | cut -d= -f2-)" \
    --image /tmp/frame.jpg        # 有真实相机画面最好；没有可以省略 --image
```

它只打**一个**接口 `POST /eval_dual`（默认 3 帧），打印每帧的 HTTP 状态、耗时、
解析出来的速度，以及原始响应体：

```
POST /eval_dual  x3  instruction='Go to the chair and stop.'  (41231 B JPEG, field 'image')
    frame 0 carries reset=true (start the episode); the rest carry reset=false (append to the history)

    frame 0: vx=+0.1667 wz=+0.0000 source=discrete_action[1] waypoints=0
    frame 1: vx=+0.1667 wz=+0.0000 source=discrete_action[1] waypoints=0
    frame 2: vx=+0.0000 wz=+0.1745 source=discrete_action[2] waypoints=0

episodes opened : 1
frames evaluated: 3
instruction     : Go to the chair and stop.
last note       : episode 1 reset for 'Go to the chair and stop.'
last response   : {"discrete_action": [2]}

PROBE OK: the server speaks the protocol this client expects
```

**它不碰 ROS、不碰 CAN、不碰轮子**，机器人 armed 着跑也没关系。

看什么：
- `PROBE OK` → 通了，进下一步；
- 连不上 / 超时 → 网络/防火墙/端口。它会直接告诉你原因
  （`Connection refused` / `timed out`），先解决这个；
- 拿到 **404** → 你连上的不是 DualVLN 服务，或者它换过路径；把原始响应贴出来；
- 报 `missing file field` 或 400 → 服务器要的图片字段名不是 `image`，
  而本客户端只发这一个（两份本地部署文档和 `vln_query.py` 里都只有 `image`）。
  这时候是**服务器版本对不上**，不是可以靠"换个字段名试试"糊过去的；
- 报 500 且提到 `instruction` → 请求里少了指令字段（本客户端每帧都带，出现说明有人手改过）；
- `invalid JSON` → 响应体不是 JSON，把原始响应贴出来。

> 合成图（不给 `--image`）能验协议，但模型对合成图的回答是胡话，别拿它判断模型好坏。

---

## 第 3 步 · 起容器、构建

```bash
bash scripts/run_container.sh vln      # 起容器 robonix_vln_dev
docker exec -it robonix_vln_dev bash   # 进容器
```

> 容器里没有 GPU：**策略跑在远端**，本机不做 CUDA 计算。
> 相机和 USB-CAN 是 USB 设备，容器里已经放开了。

容器**里面**：

```bash
cd /root/deploy
bash build.sh
```

构建完检查一件事——代码生成有没有真的产出接口（没有它 provider 起不来）：

```bash
ls rbnx-build/codegen/robonix_mcp_types/ | grep internvln
# 应该看到 internvln_navigation_control_mcp.py
```

---

## 第 4 步 · dry-run 起来，看链路

```bash
bash start.sh
```

`start.sh` 会依次：读 `.env` → **探一次 `/eval_dual`**（GET 期望 405）→ 配 CAN → 起 zenoh router → `rbnx boot`。

- 如果探不到模型而 `mode: live`，它**拒绝启动**（这是故意的）；
- dry-run 下探不到只会警告一句，继续起——但那时不会有用。

另开一个终端进容器看链路：

```bash
docker exec -it robonix_vln_dev bash
source /opt/ros/humble/setup.bash && export RMW_IMPLEMENTATION=rmw_zenoh_cpp
cd /root/deploy && python3 scripts/watch_vln_chain.py 2>/dev/null
```

打出来的表长这样（**这是 dry-run 的真实样子**；status 那几行是**发过指令之后**的样子，
没发指令时是 `armed=False  frames=0  last_note: awaiting an operator instruction`）：

```
VLN command chain   14:22:07   mode=dry-run   (Ctrl-C 退出)

topic                        pub sub  Δmsg        vx        wz  note
------------------------------------------------------------------------------------------------
/ranger/vln/cmd_vel_raw        0   1     0         -         -  dry-run: no velocity publisher by design
   模型输出：discrete_action 或 trajectory 换算成的 Twist
/cmd_vel_nav                   0   1     0         -         -  dry-run: no velocity publisher by design
   source mux 唯一输出（navigation 与 vln 二选一）
/cmd_vel                       0   2     0         -         -  dry-run: no velocity publisher by design
   chassis guard 输出 -> 底盘（只允许这一个发布者）

/ranger/vln/status  (node status, 12 msgs)
    age  0.4s  mode=dry-run  source=vln  plan=active_plan  state=running  episode=1  frames=180  flushes=8  failures=0  healthy=True
    instruction pending=Go to the chair and stop.  active=Go to the chair and stop.  reset_pending=False
    executed vx=0.0 wz=0.0  (what the node thinks it sent)
    lockstep=False  step=servo  segment=0/0  goal=-  feedback=timer  looking=yes  (nominal 1.0s + 0.5s settle)
    odometry: fresh
    servo: chasing (1.24, 0.08, 28.0deg)  err=0.214m dyaw=-2.5deg
    last_note: discrete_action[2] -> 1 step(s) over 1.00 s: turn left 15.0 deg over 1.00 s
```

**dry-run 下三个话题 `pub` 全是 0，这是对的，不是故障。** 节点故意不建速度发布者、
guard 也故意不启动——所以**没有任何进程能发出速度**。表头的 `mode=` 会先告诉你是哪种模式。

所以 dry-run 下要看的**不是这张表**，而是下面那行 status：

- `age` 一直很小、`msgs` 在涨 → 节点活着（表头写了 `mode=dry-run` 就说明它在跑，别再去看 `pub 0`）；
- `frames` 在涨、`failures=0 healthy=True` → 模型在回话且健康；
- `last_note` 每隔一会儿变一次 → 动作码真的换了（这就是端到端通了）。
  每集只写最后一帧，所以它不会每帧都变。

想看每帧的换算结果，去 `start.sh` 那个终端的日志里找 `dry-run prediction: vx=… wz=…`。
`pub=1` / `Δmsg` 在涨这些判据，**切到 `mode: live` 之后才成立**（见 `docs/ACCEPTANCE.md`）。

---

## 第 5 步 · 在 dry-run 下发一条指令

**先看清楚：在发指令之前，栈是"哑"的。** 默认 `require_instruction: true`——没有指令在手上时，
节点**一次模型都不问**，status 里是 `armed=False`、`frames=0`、
`last_note: awaiting an operator instruction`，车当然也不动。所以第 4 步里看到 `frames` 一直
是 0、`last_note` 一直不变，**不是故障**，是你还没下单。发指令是开机后的第一件正经事：

```bash
scripts/set_vln_instruction.sh "go to the chair and stop"
```

它会**立刻返回**（只入队），然后打印当前状态。回到 watcher 那个终端，
应该看到 `armed` 变 `True`、`episode=` 加一、`frames` 从 0 重新开始涨（下一帧带 `reset:true`）。

也可以走 Robonix 能力（在容器里 / 有 rbnx 的地方）：

```bash
rbnx ask -c robonix/primitive/vln/task '{"operation":"set","instruction":"walk to the door"}'
rbnx ask -c robonix/primitive/vln/task '{"operation":"get"}'
rbnx ask -c robonix/primitive/vln/task '{"operation":"stop"}'
```

`set` 会**一直等到这一轮任务结束**再返回（`status: finished` / `stopped`，或到了
`set_block_timeout_s` 仍没结束则回 `running`）。等的时候终端看着像卡住，其实是任务在跑：
一个任务一个 plan 节点，Pilot 不会在任务中途反复重派。想看中间状态用另一扇门 `get`。

**到这一步，链路已经全通了，而且机器人一步没动。** 先把这一步跑稳。

---

## 第 6 步 · armed（会动，手不离遥控器）

1. 停栈：`bash stop.sh`
2. 切模式：`scripts/set_vln_mode.sh live --yes`
   （不带 `--yes` 会被拒；导航栈还在跑也会被拒，退出码 4）
3. 自检：`python3 scripts/validate_manifest_consistency.py`（会提示 `(ARMED)`）
4. 确认导航栈已停、CAN 空闲、前方净空 10 m
5. `bash start.sh`，这时会打印：
   ```
   WARNING: mode is live -- the chassis guard will be started and this
   stack WILL drive the wheels. Keep a physical remote in hand.
   ```
6. 起 watcher，看 `/cmd_vel` 这时的 `pub` **必须恰好为 1**
7. 发**一条**指令，看它动

想看当前是哪种模式、有没有东西在跑：`scripts/set_vln_mode.sh`（= `... status`）。
切回安全状态：`scripts/set_vln_mode.sh dry-run`（安全方向，不需要确认）。

要它停：

```bash
scripts/set_vln_instruction.sh stop
```

`stop` 是保留字：它撤销计划（速度在一个 `plan_hold_s` 内归零），**不会**变成一句导航目标；
同时它**把任务也收走**（`clear_instruction`）——所以停住之后不会再拿同一个问题去问模型、
也就不会照着答案又开起来，一直等你下一条新指令（status 回到 `armed=False`）。
服务器那边没有「任务」可以作废——它不记你叫什么、也不记你在跑什么，唯一的状态是它缓存的帧历史；
下一次 `evaluate` 会带 `reset:true` 把那历史清掉。

### armed 状态下的验收清单

- [ ] `/cmd_vel` 的 `pub` **恰好为 1**（chassis guard 独占）
- [ ] `/cmd_vel_nav` 的 `pub` **恰好为 1**（source mux 独占）
- [ ] `/ranger/vln/cmd_vel_raw` 的 `Δmsg` 持续 > 0
- [ ] `|vx| ≤ 0.25`、`|wz| ≤ 0.30`（超了说明 guard 没生效）
- [ ] `vx` 从不为负（前向锁）
- [ ] 断开模型服务器（拔网线/停服务）→ **3 帧之内机器人停**，`failures` 涨到 3
- [ ] `stop` 指令 → 一个 `plan_hold_s` 之内停
- [ ] `Ctrl-C` 掉 stack → 机器人立刻停（节点 close 会先发零速度）
- [ ] 重启栈 → 默认回到 dry-run（除非你又改了 manifest）

---

## 第 7 步 · 排障

| 现象 | 先看什么 | 常见原因 |
|---|---|---|
| 起来了但 `frames=0` | status 里 `armed=` | **没发指令就是这样**（默认不武装）；发过指令还是 0 才是相机话题不对（`ros2 topic list \| grep image`） |
| `healthy=False`，`last_error` 有字 | 状态行里的 `last_error` / `last_note` | 模型服务器挂了 / 网址写错 / 防火墙 |
| 状态行完全不动 | `/ranger/vln/status` 的 age | 节点没起来（`rbnx` 日志、`logs/`） |
| `episode=` 有值但 `frames` 不动 | 相机断流 | USB 掉线、`lsusb` 看不到相机 |
| 机器人原地打转不前进 | `/cmd_vel` 那行 `vx=0 wz≠0` | 三种都正常：模型发的是动作码 2/3（纯原地转）、轨迹那条路判"比底盘最小半径还紧"、或转弯半径钳制（`clamp_yaw`）。看 `last_note` 分清 |
| 一直直走不拐弯 | `last_note` 里的 `arc radius` | 模型画的线在我们看的那 0.3 m 处本来就几乎不偏；`wz` 会是很小的非零值（宽弧），这是对的，不是没转 |
| 完全不动，`/cmd_vel` 有非零值 | CAN | `ip -details link show can_ranger`；底盘上电了没 |
| `guard` 没起 | `/cmd_vel` 的 pub = 0 且 `mode` 是 live | launch 没带起来，看 `rbnx` 日志 |
| 指令发了没反应 | 状态行 `pending=` | RPC 的 `set` 会阻塞到任务结束；看 10 Hz 的 `_pump_instruction` 有没有跑 |

抓现场（三份日志一起给开发看）：

```bash
docker exec robonix_vln_dev bash -lc 'cd /root/deploy && tail -200 logs/*.log 2>/dev/null'
docker exec robonix_vln_dev bash -lc 'ros2 topic echo --once /ranger/vln/status std_msgs/msg/String'
docker exec robonix_vln_dev bash -lc 'ros2 topic list'
```

---

## 第 8 步 · 收工

```bash
bash stop.sh          # rbnx shutdown + 检查有没有进程残留
```

`stop.sh` 会在最后 `pgrep` 一遍 `internvln_navigation.*`，**如果它打印了 WARNING，
说明有进程活过了 shutdown**，先杀掉再起导航栈。

模型服务器在远端，本仓不管它——停不停随你。

---

## 附：常用命令速查

| 想干什么 | 命令 | 在哪 |
|---|---|---|
| 离线全检 | `bash scripts/validate_offline.sh` | 宿主机/容器都行 |
| 检查 manifest | `python3 scripts/validate_manifest_consistency.py` | 同上 |
| 看/切模式 | `scripts/set_vln_mode.sh [status\|dry-run\|live --yes]` | 同上 |
| 测模型连通 | `python3 scripts/probe_internvla_server.py --url ...` | 同上（不碰 ROS） |
| 构建 | `bash build.sh` | 容器内 |
| 起 / 停 | `bash start.sh` / `bash stop.sh` | 容器内 |
| 看链路 | `python3 scripts/watch_vln_chain.py 2>/dev/null` | 容器内 |
| 发指令 | `scripts/set_vln_instruction.sh "..."` | 容器内 |
| 急停（软件） | `scripts/set_vln_instruction.sh stop` | 容器内 |
| 硬停 | `Ctrl-C` 掉 start.sh | 容器内 |
