# 验收清单 · Ranger InternVLA-N1

**每一项都要留下证据文件**；没有证据的项不能对外说"已通过"。
命令怎么敲见 [RUNBOOK.md](RUNBOOK.md)，安全前提见 [SAFETY.md](SAFETY.md)。

---

## 0. 前置（不满足就不要往下走）

- [ ] 导航栈已停：`pgrep -f 'ranger_base_node|velocity_guard'` 无输出
- [ ] `can_ranger` 空闲，且本栈是唯一 owner
- [ ] 容器 `robonix_vln_dev` 已起，仓库挂载在 `/root/deploy`
- [ ] `.env` 已填 `INTERNVLA_SERVER_URL`（**根地址**，不带 `/eval_dual`）
- [ ] 算力服务器上 DualVLN 已起，`--resize_w/h 384` 与 manifest 的 `model_crop_size` 一致
- [ ] 遥控器在手，测试区域清空 ≥ 10 m

## 1. 离线门（不碰硬件，随时可跑）

```bash
bash scripts/validate_offline.sh
```

- [ ] `internvln_navigation` 全部单测通过
- [ ] manifest 一致性 8 项通过（命令链、配置键、provider、相机接线、
      模型地址、VLN 档位、guard 上下限、无死栈）

额外的源码级断言（在单测里，属上面第一项）：

- [ ] dry-run 下 Twist 发布者数量为 0
- [ ] `/cmd_vel` 只有 `chassis_guard_node.py` 里的那一处发布
- [ ] 整包没有任何模块直接碰 `ranger_chassis` / CAN
- [ ] `worker.py`、`http_client.py`、`vln_runtime.py` 等离线模块不 import rclpy

## 2. 远端模型服务（不碰 ROS）

```bash
python3 scripts/probe_internvla_server.py --url "$INTERNVLA_SERVER_URL" --image /tmp/frame.jpg
```

- [ ] `GET /eval_dual` 回 **405**（路由在，只是不收 GET）
- [ ] `POST /eval_dual` 返回可解析的 `discrete_action` 或 `trajectory`
- [ ] 记录：单次 `/eval_dual` 的 p50 / p95 延迟（决定 `plan_hold_s` 是否合适）
- [ ] 若服务器开了 `--plan_step_gap 4`：记录"跑 planner"与"跳过"两种调用的延迟差异
- [ ] 第一帧带 `reset:true`、后续帧带 `reset:false` 时，服务器确实是"新 episode / 续历史"两种行为
- [ ] 同一条指令跑久了，状态里 `memory_flushes` **保持 0**、`episode` **不变**——
      `history_reset_frames` 默认 0（不清历史，和官方客户端一致）。若把它设成非 0，每 N 帧
      `memory_flushes` 加一、`last_note` 带 `memory flushed`，那是清历史不是新任务
- [ ] 状态里 `odom.received=true` 且 `odom.fresh=true`，`step.feedback=odom`——
      `/odom` 没接上时 `feedback` 会一直是 `timer`，闭环等于没开（会退回纯计时）
- [ ] 现场量一次：给 `Turn left.` 应该是 15°，从 `step.goal=turn +15.0 deg` 到
      `step_done` 的墙钟时间**不再等于** `action_step_s`（底盘转够了就提前结束）。
      量法是前轮贴个标记、数实际转过的角度；闭环生效时误差应 ≤ 2°

## 3. 相机与话题

- [ ] `/camera_435i/color/image_raw` 有数据，约 30 Hz
- [ ] 帧能解码成 JPEG（节点日志无 `camera frame rejected`）
- [ ] manifest 的 `rgb_topic` 与实际话题**一字不差**
- [ ] `enable_depth: true`，`/camera_435i/depth/image_raw` 有数据（深度**不进模型**，
      只喂近距闸门；关掉它 live 下等于车不动）
      ——**当前部署不适用**：`safety_gate_enabled: false`，`enable_depth` 也相应为 `false`。
      重新打开闸门时两条一起改，再验这两项。
- [ ] manifest 的 `depth_topic` 与实际深度话题一字不差（仅在闸门开启时适用）
- [ ] 日志里没有 `depth frame rejected`（仅在闸门开启时适用；有的话说明编码不是 16UC1/32FC1，闸门会闭锁）

## 4. dry-run（默认，机器人不动）

> 这一节的判据**只能看 `/ranger/vln/status`**。dry-run 下节点故意不建速度发布者、
> guard 也故意不启动，所以 `cmd_vel_raw` / `cmd_vel_nav` / `cmd_vel` 三个话题的
> `pub` **全是 0、`Δmsg` 永远是 0**——那不是故障，是设计。watcher 表头会写 `mode=dry-run`。

- [ ] `/ranger/vln/status` 每秒一条，`mode=dry-run`，`age` 一直很小
- [ ] `frames` 持续在涨（模型每帧都答上了）
- [ ] `failures=0`、`healthy=True`
- [ ] **`/cmd_vel` 上 publisher 数为 0**（硬要求：dry-run 不发速度）
- [ ] `start.sh` 终端里 `dry-run prediction: vx=… wz=…` 随画面变化
- [ ] 发一条指令：`episode=` 加一、`frames` 从 0 重新计数（下一帧带 `reset:true`）
- [ ] 发第二条指令：在途的旧结果被丢弃（`last_note` 变成
      `discarded a plan for a superseded instruction`），不会拿旧任务的答案去开底盘
- [ ] 发 `stop`：计划立即撤销（`plan=` 不再是 `active_plan`）
- [ ] 让模型一直回答 STOP（或对着已到达的画面发一条已完成的指令）：连续 ≥ 20 帧
      **且**持续 ≥ 30 s 后，`vln/task get` 回 `status="finished"` / `running=false`、
      `instruction` 是那条任务；`frames` 停止增长（不再请求模型）。不足这两个条件
      （例如只回了十几次 0）**不会**结束；期间来一帧前进动作会把计数和计时一起清零
- [ ] 前方净空时 `status.safety.reason == "clear"`，`nearest_m` 是一个合理距离
      ——**当前部署不适用**：闸门关闭时 `status.safety` 里 `enabled=false`，不订阅深度
- [ ] **手伸进前向带**（画面中间、别贴镜头）：≤0.30 m 时 `safety.blocked` 变 `true`、
      `reason` 是 `obstacle`，`dry-run prediction` 那一行 `vx=0.000 wz=0.000`；
      手拿开后要离开 0.50 m 才恢复 `clear`（回差是故意的）——**闸门开启时才验**
- [ ] 挡住的同时**原语被停掉**：`robonix/primitive/vln/task get` 返回
      `status="blocked"`、`running=false`、`detail` 里有 `reason=obstacle` 和
      `no model requests`；`frames` **停止增长**（worker 不再问模型）；
      `/ranger/vln/status` 的 `operator.safety.blocked=true`、`stops` 加一
      ——**闸门开启时才验**
- [ ] 挡住时发一条 `set`：返回 `status="held"`，`frames` 仍不涨；
      手拿开超过 0.50 m 后这条指令**自动开始**（`frames` 重新涨、`episode` 加一），
      不用重发
- [ ] 放行后刚才被撤销的那个任务**不会**自己恢复：`get` 回到
      `awaiting-instruction`，要动得重新发指令
- [ ] 把深度话题停掉/挡住：0.30 s 后 `reason` 变成 `stale_depth` 或 `no_depth`，
      `blocked=true`，`vln/task get` 同样报 `blocked`——这就是 live 下的"没有深度就不许动"

## 5. 命令链（切 live 后，逐段验，断在哪段就往回查）

- [ ] `/ranger/vln/cmd_vel_raw` 有数据，`vx ∈ [0, 0.25]`、`wz ∈ [-0.30, 0.30]`
- [ ] `/cmd_vel_nav` 有数据（source mux 的唯一输出）
- [ ] **`/cmd_vel` 上 publisher 数恰好为 1**（只有 chassis guard）
- [ ] `/cmd_vel` 的 `vx` **从不为负**（前向锁）
- [ ] `/cmd_vel_nav` 上 publisher 数恰好为 1
- [ ] 底盘确实在动，且**没有"原地打转不前进"**（转弯半径钳制在生效）
- [ ] `guard` 日志里能看到 `clamp_yaw` 的记录
- [ ] 全程 `linear.y == 0`（底盘没有切换 4WS 转向模式）
- [ ] 前方有人/物进入 0.30 m：`/cmd_vel` **立刻**变成 `vx=0 wz=0`，`vln/task get`
      变成 `status="blocked"`；人走开超过 0.50 m 后闸门放行，但**任务需要重发**
      （被安全停掉的任务不会自己重启；挡住期间发的指令会 `held` 并在放行后自动开始）
      ——**当前部署不适用**：闸门已关闭，这条验不了。要验先把
      `safety_gate_enabled` 和相机的 `enable_depth` 一起打开。
- [ ] 一条"直走"指令：`vx > 0`、`wz ≈ 0`
- [ ] 一条"左转"指令，**三种回包都算通过**：回动作码 2/3 → `vx == 0`、`wz > 0`（纯原地旋转）；
      回轨迹且方位角 ≤ 10.7° → `wz > 0` 且 `vx > 0`（走一段左弧，`last_note` 里有 `arc radius`）；
      回轨迹且方位角 > 10.7° → `vx == 0`、`wz > 0`、`last_note` 里写 `pivoting`

## 6. 失败行为（每条都要主动构造一次）

| 构造方式 | 期望 | 证据 |
|---|---|---|
| 停掉远端服务器 | `failures` 涨到 3 → 计划被撤销 → **机器人停** | watcher 截图 + 日志 |
| 拔相机 | `frames` 停止增长 → 计划到期 → **停** | 同上 |
| 拔深度（或 `ros2 topic hz` 停掉深度） | 0.30 s 内 `safety.blocked=true` → **停**；任务撤销、`vln/task` 报 `blocked` | `status.safety` + `vln/task get` |
| 发一条指令后立刻发 `stop` | 不执行前一条 | `status` 里的代际/计划记录 |
| `Ctrl-C` 掉 `start.sh` | 先发零速度再退出 | 日志 |
| `bash stop.sh` | 无进程残留（脚本不打印 WARNING） | 终端输出 |

## 7. 长稳

- [ ] ≥ 10 min dry-run 无 OOM、无 thermal throttle
- [ ] ≥ 5 min live 运动窗口内三路命令连续、无中断
- [ ] `tegrastats` 记录 RAM / SWAP / 温度（本机不做 CUDA，看 CPU/内存即可）
- [ ] 停止后 `/cmd_vel` 回到 0，且退出后**不再有发布者**

## 8. 对本体的回归（硬性）

- [ ] 本栈关掉后，**导航栈能正常起来**（建图/定位/导航都正常）
- [ ] 参照仓 `skill-follow-person-rbnx`、`robot-agilex-ranger_mini_v3` **零改动**

      注意：这两个仓**本来就是脏的**。`skill-follow-person-rbnx` 对 HEAD 有 17 个 `M`
      和一个 `?? config/nav2_params.yaml`，`robot-agilex-ranger_mini_v3` 有 1 个 `M`——
      这些改动都早于本次工作（最新 mtime 2026-09-29，HEAD 停在 2026-09-17），
      本仓是从它们**当时的工作区**派生出来的。

      所以"零改动"不能拿 `git status` 是否为空来判断，要跟**开工前自己拍的快照**比：

      ```bash
      find <参照仓路径> -newermt 2026-10-10 -not -path '*/.git/*'
      # 应该没有任何输出
      ```

## 9. 语音（可选，**底盘完全不动也能验**）

语音不是验收硬门槛——它不通只影响"用嘴下指令"，导航照旧。但如果要验，按这个顺序：

- [ ] `rbnx build` 通过，且构建日志里有两行：
      `Package 'robonix.primitive.audio.client_bridge' build finished`、
      `Package 'com.robonix.example.speech_service' build finished`
- [ ] `.env` 里有三个 `TENCENT*` 变量（值不回显、不进 manifest）
- [ ] 栈起来后 `rbnx list`（或 atlas 查询）能看到 `audio_client_bridge` 的
      `robonix/primitive/audio/mic`、`.../speaker` 和 `speech` 的
      `robonix/service/speech/listen`、`.../speak`
- [ ] 机器人 60002 在监听：`ss -lntp | grep 60002`
- [ ] 一台装了 robonix-client 的电脑连上 60002 后，录一句话能在日志里看到 ASR 文本
- [ ] **断连测试**：client 不连时栈照常起、`vln/task` 照常工作，日志里没有语音反复重连的刷屏
- [ ] **失败隔离**：把 `TENCENTCLOUD_SECRET_KEY` 改错重启，语音报鉴权失败，但
      `/ranger/vln/status` 与底盘行为**不变**

> 语音链路是"client → bridge → ASR → pilot → `vln/task`"。pilot 的 VLM 没配好或模型服务
> 连不上时，现象是"听得到、车不动"——这时先查 pilot，别怀疑 ASR。
> 中文指令的意图路由靠 VLM，ASR 本身不保证被路由成导航任务。

## 证据留存

- `git rev-parse HEAD`（本仓 + 模型仓 revision）
- `uname -a` / L4T / ROS / Python / rclpy 版本；模型服务器侧的 CUDA/torch 版本
- 相机话题频率、最大帧间隙
- 模型 `/eval_dual` 的 p50 / p95 与实测动作频率
- `tegrastats` 功耗与温度轨（仅本机）
- 各段 cmd_vel 话题的计数与取值范围
- 每次失败的构造方式 + 机器人实际行为（停没停、多久停）
- 起始/结束时间、指令序列、最终状态

## 与参照（MiniCPM-RobotTrack 跟随）的差异（逐条说明）

参照 `skill-follow-person-rbnx` 的 follow profile 渲染出的清单是：
system `atlas/executor/pilot/liaison/soma/vitals`；
primitive `robot_description/mid360_lidar/mid360_imu/front_camera/ranger_chassis/audio_client_bridge/go2_robottrack`；
service `speech/mapping/nav2`。

| 参考项 | 本仓 | 说明 |
|---|---|---|
| `go2_robottrack` | **`internvln_navigation`** | **本次唯一的功能替换**：MiniCPM-RobotTrack → InternVLA-N1 DualVLN |
| `pilot` | **包含** | 对话核心；语音链路里由它的 VLM 把一句中文路由到 `vln/task`。文本模式下它不参与导航 |
| `speech`（Tencent ASR/TTS） | **包含**（可选链路） | 语音输入/播报；不在控制路径上，没配凭据时只有语音不可用 |
| `mid360_lidar` + `mid360_imu` | **未包含** | 参考用它们喂 RTAB-Map；DualVLN 只吃一帧 RGB |
| `mapping`（RTAB-Map） | **未包含** | 依赖雷达；本 profile 没有雷达 |
| `nav2` | **未包含** | 参考只要它的 velocity smoother + velocity guard；本包的计划过期与 50 Hz 派发已承担同样的节奏控制 |
| `audio_client_bridge` | **包含** | client 侧麦克风/音箱，反向 WebSocket 60002；没有 client 连接时只是哑的 |
| `front_camera`（Orbbec） | **保留**，当前只开彩色 | 彩色喂模型；深度原本只喂近距反射闸门（0.30 m 停车），**不进模型**。闸门现已关闭，深度流也一并关掉 |
| `ranger_chassis` + chassis guard | **保留**，逻辑零改动 | 底盘适配层与模型无关，原样继承 |
| `robot_description` | **保留** | 与模型无关 |
| `source_mux` / `worker.py` / `image_preprocess.py` | **保留**，逻辑零改动 | 只把源名字 `robottrack` → `vln` |
| 固定距离跟随（depth + HOG 定距） | **删除** | 那是 follow 的功能，不是 navigation 的 |
| `vln_bridge.py`（Flask 转发） | **不引入** | 直接对官方 server；动作码换算搬成包内纯函数 `action_adapter.py` |

## 当前状态（诚实口径）

### 已在开发机上跑过的（第 1、2 节的可离线部分）

不是在机器人上，是在一台 Windows 开发机上，用 Python 3.12 + PyYAML + numpy + cv2：

- ✅ 包内单测 **144 项全过**（`Ran 144 tests / OK`）
- ✅ manifest 一致性 **18 项全过**（`manifest consistency OK`）
- ✅ `eval_dual` 客户端**真的起了 socket 跑过**：用一个按本地文档协议写的桩服务器，
  验了 `POST /eval_dual`（`discrete_action` 与 `trajectory` 两种回包），
  含 `reset` 时序（首帧 true、续帧 false、换指令再 true、失败后重 true）与失败升级
- ✅ `probe_internvla_server.py` 三种情形都跑过：服务器正常 / 只认 `image` 字段 / 端口没人听
- ✅ `start.sh` 的预检真跑过：漏配 URL、URL 带 `/eval_dual`（含尾斜杠）、
  live 且模型不可达 → 都**确实拦住了**；dry-run 且不可达 → 只警告
- ✅ 全部 shell 脚本过 `bash -n`，全部 Python 过 `py_compile`

**但桩服务器不等于真实服务器**：它只是照着两份本地部署文档写的，
算力服务器上那份（官方名 `scripts/realworld/http_internvla_server.py`）还没被这个客户端碰过一次。
上车前必须按 [DEPLOY.md](DEPLOY.md) 4.2 的两条 curl 确认 `POST /eval_dual` 在、且能出一帧动作。

### 完全没验的（第 3～8 节，全待跑）

- ⚠️ **任何需要 ROS / rclpy / cv_bridge / 底盘 / CAN 的部分一次都没跑过**
- ⚠️ **没有任何实机验证**——本机不是机器人
- ⚠️ `plan_hold_s = 1.5` 仍是看门狗估算；实测回答间隔 p50 0.778 s / p90 0.915 s（2026-10-22 live 日志），留了余量
- ❗ **连续驱动（`servo_enabled`，默认开）= 真机行为变了**：车不再"走一步停一下等答案"，
  而是一直朝最新的目标位姿走，答案只负责挪目标点。现场第一次跑要确认三件事：
  〔1〕`/ranger/vln/status` 的 `servo` 在 `chasing` ↔ `goal reached` 之间来回切，
  不是一直 `idle`；〔2〕走直线时 `/cmd_vel` 的 `vx` **不出现归零的坑**（以前每个周期
  有 1 s 以上是 0）；〔3〕**把手放急停上**——中途如果 odom 掉了，车会停在 `servo_wait_odom`，
  这是设计，不是卡死。回退：`servo_enabled: false`（配合 `lockstep_enabled: true` 回到走走停停）
- ❗ **一步一决策（2026-10-29 定，改回默认 1）**：模型回 `[1,1,1,1]` 也只走 **1 步**（0.25 m），
  然后停车拍新帧。想一次走 1 m 得把 `max_chunk_actions` 调到 4——**本栈每一步都是开环盲走、
  也没有避障**，4 步等于一次决策盲走 1 m，现场真要开请先确认前方 1.5 m 净空
- ❗ **纯追踪换算（2026-10-29）**：轨迹路线上弯上转得比以前**快约一倍**（旧公式把半径算成了两倍）。
  分界点也变了：方位角超过 **±10.7°** 就改原地转（旧值是 20.3°）。现场第一次跑要**把手放急停上**
- ⚠️ `guard_min_turn_radius_m = 0.81` 来自厂商手册而非实测，需在第 5 节标定
- ⚠️ `trajectory` 的点是否带 yaw 未确认（解析器接受三元组但只用 x/y）
- ⚠️ 相机话题前缀按 `camera_name: camera_435i` 推定，上车后用 `ros2 topic list` 复核
- ⚠️ 包内 `max_vx` 默认是 0.15（保守值）：动作 1 会被驱动 1.67 s 而不是 1.0 s
  （位移仍是 0.25 m，限速只拉长时长）。部署用的是 manifest 里的 0.25，所以只在
  "不走 manifest 单独跑包"时才会看到
