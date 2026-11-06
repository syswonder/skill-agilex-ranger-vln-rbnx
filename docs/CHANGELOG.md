# 变更记录

本仓的改动台账。**每次改代码就加一行**，别攒着——这个仓的坑大多数是"文档说四个接口、
真服务器只有一个"这类**认知错位**，忘了当时为什么改就会改回去。

---

## 2026-11-04 · 第二次改名：`skill-internvln-navigation-rbnx` → `skill-agilex-ranger-vln-rbnx`

**用户要求**：按上次同样的方式再改一次名，文档与容器一起同步。

| 旧 | 新 |
|---|---|
| `/mnt/ssd/wxf/jiaofu/skill-internvln-navigation-rbnx` | `/mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx` |

**改了什么**：

| 文件 | 变化 |
|---|---|
| 目录 | `mv` 到新路径（同一文件系统，inode 不变） |
| `README.md`、`docs/COMMANDS.md`、`docs/DEPLOY.md`、`docs/OVERVIEW.md`、`docs/RUNBOOK.md` | 旧仓名逐处替换（含 Windows 开发机路径示例） |
| `scripts/run_container.sh` | `vln)` 分支的 `DEPLOY=` 改新路径（唯一的功能性硬编码） |
| 兄弟仓 `skill-follow-person-rbnx` | 无引用，无需改 |

**CHANGELOG 历史条目里的旧名保留原样**：那是当时真实的路径。本表就是改名映射。

**容器**：bind mount 记的是旧路径。目录改名后旧容器**当下还能跑**（挂载按 inode），但任何
restart 都会用旧路径重建一个空目录，`/root/deploy` 就空了。必须用新路径重建
`robonix_internvln_dev`（镜像 `robonix:humble-orin-follow`、`--init --network host`、
`-v /dev:/dev`、`c 189:* rmw`、无 GPU，参数与上次一致）。

> **状态：待执行。** 本次工具侧的 stop/rm/run 被权限拦截，旧容器仍在跑；下面的命令由操作员
> 在宿主机执行。执行前确认容器内没有在跑的栈（本次检查时没有）。

```bash
sudo docker stop robonix_internvln_dev && sudo docker rm robonix_internvln_dev
sudo docker run -d --name robonix_internvln_dev --init --network host \
  -v /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx:/root/deploy \
  -v /mnt/ssd/wxf/jiaofu/robonix:/root/workspace \
  -v /dev:/dev --device-cgroup-rule "c 189:* rmw" \
  robonix:humble-orin-follow
```

（容器名保留 `robonix_internvln_dev`，文档和脚本都按这个名字引用；重建时栈没在跑。）

**验证**：改名后 `grep` 旧仓名只剩本文件的历史条目；`validate_manifest_consistency.py` OK。

回退：`mv` 回原名，把上面六个文件的字符串和 `run_container.sh` 的 `DEPLOY=` 改回；容器再用
旧路径重建一次。

---

## 2026-11-04 · 模型一直回 0 时 Pilot 无限重派任务 + 执行端过冲（改在原语侧）

**用户反馈**：模型连续给 STOP 已经是"任务结束"了，Pilot 还在反复触发新的
`internvln_navigation` 任务；另外车执行指令时"方向盘打太大、油门踩太深"，不是错过目标就是撞墙。

**硬约束**：只改本仓（internvln 原语），**不动 robonix 源码**。道理是 VLN 服务于 robonix 的
调度，不是反过来让调度迁就 VLN。

### 一、重派任务：三段循环，根子都在原语

链路是 Pilot → Executor → `robonix/primitive/vln/task set`。查下来是三个原语侧的洞叠在一起：

1. **`set` 立刻返回**。原实现只把指令入队就回 `queued`，plan 节点毫秒级完成；Pilot 看到
   `PlanDone` + `interaction_active` 就重新规划、重新 `set`——一个任务被拆成几十个毫秒级的 plan。
2. **重复 `set` 无条件清掉完成闭锁**。哪怕指令一字不差，`_finished` 也被抹掉、episode 重开。
3. **执行端看不到"任务已经结束"**。完成态只活在下一次 `set` 之前的那一瞬间。

**改法**（`vln_runtime.py` / `provider.py`）：

- `set` 改成**阻塞到本轮 episode 终结**（`finished` / `stopped`，或超时
  `set_block_timeout_s`，默认 900 s）。这样 plan 节点活多久 = 任务活多久，Pilot 不会在任务进行中
  反复重派，任务结束后能拿到 `status="finished"` 自己收尾（按 Pilot 提示词，反馈足够时它应把
  `task_update.status` 置 done 并输出空 RTDL）。
- **完成回显抑制**（`finish_reissue_grace_s`，默认 10 s）：任务刚 finished 时，同一句指令再
  `set` 不重开，直接回 `status="finished"`、`running=false`。
- **运行中同指令幂等**：同一句指令在跑，回 `already_running`，不开第二个 episode。
- **操作员 `stop` 是终点事件**：`terminal_kind="stopped"`，清零闭锁；stop 后再 set 同一句是
  真的重跑（人要求的），不按回显处理。

`get` 走同一份 `_status_response()`，字段新增 `finished_at` / `terminal_kind` / `terminal_at` /
`terminal_instruction` / `finish_reissue_grace_s` / `set_block_timeout_s`。

### 二、过冲：速度是"前视距离 ÷ hold 时间"推出来的，必然顶到上限

- 轨迹类回答的 `wanted_vx = advance / hold`，`advance` 是整段前视（1 m）、hold 3–4 s →
  ≈1.0 m/s，再被 `max_vx` 一律裁成 0.25，同时又被拉长到 4 s——**油门到底、一给就是一大段**。
- `goal_from_waypoints` 把目标放在整段 1 m 前视的尽头，servo 追一个远处的点。
- `servo_kp_trans/yaw` 2.0/1.5，误差 12.5 cm / 11° 就饱和——**方向盘一把打到底**。
- `max_chunk_actions: 4`——一次回答承诺 1 m。

**改法**：

| 参数 | 旧 → 新 | 作用 |
|---|---|---|
| `trajectory_cruise_vx` | 新增 0.18 | 轨迹回答的前进速度直接由它决定，不再从 `advance/hold` 反推 |
| `trajectory_advance_m` | 新增 0.35 | 轨迹目标最多放在 0.35 m 外（以前是整段前视） |
| `servo_kp_trans` / `servo_kp_yaw` | 2.0/1.5 → 1.2/1.0 | 跟线更软，误差要到 20 cm / 30° 才饱和 |
| `max_chunk_actions` | 4 → 2 | 一次回答最多半步，剩下的等下一帧 |

轨迹 plan 的 servo 速度上限用 `trajectory_cruise_vx`，离散/程序 plan 仍用 `max_vx`。

### 改到的文件

| 文件 | 变化 |
|---|---|
| `packages/internvln_navigation/internvln_navigation/vln_runtime.py` | 完成回显抑制、运行中同指令幂等、stop 终点化、`set` 阻塞所需字段与 `configure` |
| `packages/internvln_navigation/internvln_navigation/provider.py` | `set` 阻塞等待 episode 终结；`get` 复用 `_status_response()`；docstring 说明"plan 节点必须活到任务结束" |
| `packages/internvln_navigation/internvln_navigation/core.py` | 新增 4 个配置项与解析；servo 增益、chunk 上限新默认值；`goal_from_waypoints(advance_m=...)` |
| `packages/internvln_navigation/internvln_navigation/action_adapter.py` | `trajectory_to_velocity(cruise_vx=...)`，巡航速度封顶 |
| `packages/internvln_navigation/internvln_navigation/ros_node.py` | 透传新配置；轨迹 plan 用巡航速度作 servo 上限 |
| `config/internvln_navigation.yaml`、`robonix_manifest.yaml` | 新键 + 注释，两层同步 |
| `tests/test_core.py`、`test_action_adapter.py`、`test_vln_runtime.py`、`test_static_contract.py`、`test_provider_contract.py` | 覆盖新行为（完成回显、幂等、stop 终点、blocking set、巡航速度、advance 上界、chunk=2） |
| `capabilities/lib/.../VlnTask.srv`、`CAPABILITY.md`、`ARCHITECTURE.md`、`COMMANDS.md`、`DEPLOY.md`、`RUNBOOK.md`、`README.md`、`scripts/set_vln_instruction.sh` | 同步"`set` 阻塞到任务结束、`get` 永不阻塞"的新契约（示例指令统一成目标句） |
| `scripts/validate_manifest_consistency.py` | 轨迹那行改为报告巡航速度与 advance 上界 |

### 验证

- `python3 -m unittest discover -s tests -q`：330 跑，6 红。全部在
  `test_set_vln_mode.ModeSwitchTests`，原因是 manifest 当前 `mode: live` 且真实
  `ranger_base_node` 正在跑（脚本拒绝在底盘栈活着时 arm）——与本次改动无关。
  其中新增的 `test_a_bound_set_waits_until_the_episode_finishes` 直接钉住"set 不提前返回"。
- `python3 scripts/validate_manifest_consistency.py` → `manifest consistency OK`（65 个配置键全部识别）。

### 回退

仓库不是 git，按下面改回去即可（都在 `config/internvln_navigation.yaml` 与
`robonix_manifest.yaml` 两层同步）：

1. **只退回"不阻塞的 set"**：`set_block_timeout_s: 0`。`set` 立即返回，Pilot 会恢复原来的
   毫秒级 plan 循环——只在这条确实要放弃时用。
2. **只退回"允许 echo 重开"**：`finish_reissue_grace_s: 0`。完成闭锁仍生效，只是刚 finished
   的同句 `set` 会重开任务。
3. **退回原来的手感**：`servo_kp_trans: 2.0`、`servo_kp_yaw: 1.5`、`max_chunk_actions: 4`、
   `trajectory_advance_m: 1.0`、`trajectory_cruise_vx: 0.25`（或删掉后两项，旧逻辑会按
   `advance/hold` 推算速度）。
4. 代码级的三个行为（echo 抑制 / 幂等 set / stop 终点化）没有开关，要完全回到旧代码得把
   `vln_runtime.py` 的 `set_instruction` 恢复成"无条件清 `_finished` 再入队"、`provider.py` 的
   `set` 恢复成"入队即返回"。CHANGELOG 里这段就是当时的 diff 语义。

**生效前提**：改的是配置与代码，必须**重建/重启栈**才生效。重启前按既定安全约束人不在现场要
先 `scripts/set_vln_mode.sh dry-run --yes`（manifest 现在是 `live`）。

---

## 2026-11-04 · 仓库改名（含路径引用同步）

**用户要求**：两个本体项目改名，且不能瘫痪；刚写的文档里的旧名字要同步改掉。

| 旧 | 新 |
|---|---|
| `/mnt/ssd/wxf/jiaofu/robot-internVLN-ranger` | `/mnt/ssd/wxf/jiaofu/skill-internvln-navigation-rbnx` |
| `/mnt/ssd/wxf/jiaofu/robot-unitree-ranger`（跟随栈，兄弟仓） | `/mnt/ssd/wxf/jiaofu/skill-follow-person-rbnx` |

**改了什么**：

| 文件 | 变化 |
|---|---|
| 全仓 `*.md` | 旧仓名逐处替换（README、OVERVIEW、RUNBOOK、COMMANDS、DEPLOY、CHANGELOG、SAFETY、ACCEPTANCE）；连兄弟仓旧名也一并换成新名 |
| `scripts/run_container.sh` | `DEPLOY=` 改新路径（唯一的功能性硬编码） |

**容器**：旧容器 `robonix_internvln_dev` 的 bind mount 记死了旧路径，改名后**任何 restart 都会
把旧路径重建为空目录再挂上**。所以没有原地重启，而是按同样的参数（镜像
`robonix:humble-orin-follow`、无 GPU、`--init --network host`、USB cgroup 规则）用新路径重建，
并保留容器名 `robonix_internvln_dev`。

**最终态（用户要求“基于最新镜像重跑、删掉老容器”）**：两个容器都从本机最新镜像
`robonix:humble-orin-follow`（`6bb7ba364760`，2026-10-13）重建——`robonix_follow_dev` 与
`robonix_internvln_dev`；挂载源均为改名后的新路径，旧容器已全部删除。

**验证**（容器内，栈未启动）：`bash scripts/validate_offline.sh` → 322 跑、5 红，全部是
`test_set_vln_mode.ModeSwitchTests` 的环境性红点（manifest 当前 `mode: live`），与改名无关。

回退：`mv` 回原名，并把 `run_container.sh` 的 `DEPLOY=` 改回；容器用旧参数重建即可。

---

## 2026-11-04 · 根目录 `README.md` 改成完整的命令手册

**为什么**：原 README 讲的是"这个栈怎么搭的"（架构关系、命令链），不是"我要敲什么"。
命令散在 `docs/COMMANDS.md`、`DEPLOY.md`、`RUNBOOK.md` 和各个脚本的 `usage` 里，新上手要
翻四五份文档。

**改了什么**：

| 文件 | 变化 |
|---|---|
| `README.md` | **重写成命令手册**（12 节：红线 / 约定 / 入口脚本总表 / 起停 / dry-run 全流程 / 切 live 与退出码 / 日志 / 话题与 status 字段 / 改配置 / rebuild / 校验与测试 / 排障速查 / 文档导航）。命令一律带当前真实值，写清"宿主机还是容器内" |
| `docs/OVERVIEW.md` | **新建**，内容是原 README 的逐字备份（架构图、命令链、脚本各自做什么），不丢历史 |

**没改代码**。手册里如实保留了几处本来就存在的口径不一致，没有替它们"圆"：

- 实际在用的容器是 `robonix_internvln_dev`（手工起的），`run_container.sh vln` 建的是
  `robonix_vln_dev`——手册让读者先 `docker ps` 确认；
- manifest 当前是 `mode: live`，而多处文档把 dry-run 写成默认；
- `rbnx ask` 在包内 README 与 provider 文档里是两种写法，手册两种都列了；
- `.env` 里的 `INTERNVLA_INSTRUCTION` 代码不读，改指令只能 `set_vln_instruction.sh`。

回退：把 `docs/OVERVIEW.md` 拷回 `README.md` 即可恢复原样。

---

## 2026-10-30 · 目标在视野里却错过、车走 S 形：三处执行端的问题

**用户反馈**："目标明明出现在视野中却丢了"、"车依然走 s 型，会错过目标"。问的是三选一：模型
不行、我们请求有问题、还是模型给的指令我们没执行到位。

### 结论：请求没问题，模型也在指着目标；错在我们的**执行**，三处

**排查用的证据**：现场 982 s 的录制（`logs/vln-capture/`：每帧图 + 每帧 status + odom）。
分离出来的三个事实：

1. **答话节奏比前视距离长**。相邻两次答话间隔：中位 1.0 s，p90 3.0 s；这段时间车走
   0.15–0.75 m。而 `lookahead_m` 是 0.30 m——**45% 的帧里被追踪的点已经在车身后方**，
   另有 62% 的帧里不到 8 cm。车在追一个自己已经开过去的点，这就是 S 形；追着过去的点当然
   会从能看见的目标旁边擦过去。
2. **一次答话的整串动作被砍成一步**。`max_chunk_actions: 1`：模型回
   `discrete_action[3, 3, 3, 3]`（连着转四次，约 60°），我们只转 15°，重拍一帧，模型只好
   再说一遍 `[3,3,3,3]`。模型已经做出的决定被拆成一串小修正，转向永远慢半拍。
3. **status 里没有"实际下发的速度"**。payload 有 `plan`/`step`/`servo`（全是意图），唯独
   没有节点真正 publish 出去的那个 vx/wz，所以"底盘有没有照命令走"只能靠猜。

同时排除了另外两条：
- **指令没写错**：同一帧换一组指令（"去红椅子"→ `[3,3,3,3]` 向左转，"去绿植"→ +11.3°），
  输出的方向跟着指令变，说明模型确实在按指令人肉定位；
- 录制里还有一次 40°/s 的自转（约我们 `guard_max_wz_rps` 0.30 的 2.4 倍）和画面里第二台
  轮式四足——**疑似被外部撞了一下**，与本改动无关；以及一次 61 s 的服务端卡死，那期间刹车
  （plan 超时归零）是正常工作的。

### 改动

| 文件 | 变化 |
|---|---|
| `core.py` | `RuntimeConfig.lookahead_m` 0.30 → **1.00**；`max_chunk_actions` 1 → **4**；`goal_from_waypoints` / `PlanStore.__init__` 的默认值同步 |
| `action_adapter.py` | `trajectory_to_velocity` 的 `lookahead_m` 默认 1.00；模块 docstring 里"为什么只执行一步"那段改写（单步盲走的理由在 `odom_feedback_enabled` 之后已经不成立） |
| `config/internvln_navigation.yaml` | `lookahead_m: 1.00`、`max_chunk_actions: 4`，注释写明两个数字各自的实测依据 |
| `robonix_manifest.yaml` | 同上（生效的那一份） |
| `ros_node.py` | `_dispatch` 里记下过闸门后**真正要 publish 的** `command`，`_publish_status` 新增 `command: {vx, wz, reason, blocked, published}`；`published=false` 表示 dry-run 下"本来会发什么" |
| `docs/ARCHITECTURE.md` | 纯追踪那节补一段"前视距离要盖过一次答话间隔"，0.30 m 的旧数字改掉 |
| `tests/test_action_adapter.py` | 两条按旧默认值写的用例改成新默认：整串执行、超过 cap 才截断；`[0,1,1,1]` 现在执行 3 步 |

**为什么是 1.00 m 而不是更大**：它刚好盖住 p90（3 s）的答话间隔在 `max_vx` 0.25 m/s 下走过的
0.75 m。这不是转向增益：同一条曲线取远一点，读到的方位角更小、弧更平，但半径 `L/(2sinα)`
仍然对得上。副作用是原地转的判据跟着变：`lookahead` 1.0 m 下，方位角在 ±38.1° 以内都是
0.81 m 以上的弧（之前是 ±10.7°），即**更少原地转、更多走弧**，这正是要的方向。

**为什么是 4**：服务端一次就回 4 个（`for command in commands: _chassis.move(...)` 也是整串
走完才拍下一帧），4 就是训练时的 chunk 长度。cap 决定"一次答话最多执行几步"，不改变顺序；
更长的串仍在 4 处截断，剩下的等下一帧。

### 验证

- `python3 -m unittest discover -s tests -q`（在 `packages/internvln_navigation` 下跑）：
  **322 跑，6 红**，全部是 `test_set_vln_mode`——它们红的原因是 manifest 现在是 `mode: live`
  且栈在跑，属环境，与本改动无关（切回 dry-run / 停栈即绿）。
- `python3 scripts/validate_manifest_consistency.py` → OK。其中两行是这次改动的直接读数：
  `one answer commits at most 4 action(s)`、`1.0000 m/s over a 1.0 m lookahead ... past
  +/-38.1 deg it pivots in place instead`。

### 一个已知的相互作用（没改，但要盯着）

`max_plan_age_s` 仍是 1.5 s（它必须等于 `plan_hold_s`），而轨迹型 plan 的 `hold` 只等于它
（离散串会按节目时长延长，轨迹串没有节目）。也就是说：**两次答话之间一旦超过 1.5 s，刹车
照样归零**，即使目标是 1.0 m 外还没走到。这是设计中的刹车（模型/链路死了必须停），不为了
"走满 1 m"去松动它；现场看 `status.plan.reason == "expired"` 就能知道这一帧是不是被它切的。
如果下一轮实测发现 p90 那 10% 的停顿明显影响观感，再单独讨论，不混在这次里。

### 能回退吗

能，都是数值/新增字段：三个 config 位置改回 `lookahead_m: 0.30`、`max_chunk_actions: 1`，
`core.py`/`action_adapter.py` 的默认值同理；`command` 字段加在 status 里只增不改，留着无害。
测试里那两条改回旧断言即可。**本仓不是 git 仓，没有 `git revert`，回退靠这份记录。**

### 生效前提

这些是**启动时读的配置**，必须重启栈才生效（`bash stop.sh` → `bash start.sh`）。manifest 现在
是 `mode: live`，重启后底盘是 armed 的——按既定约束，人不在现场就先
`scripts/set_vln_mode.sh dry-run --yes`。

---

## 2026-10-30 · `stop.sh` 兜底：没 state 文件也要能停

**现场**：在车上一路 `bash start.sh`，第一条就 `[FAIL] atlas listen address '0.0.0.0:50051' is taken`；
接着 `bash stop.sh` 只回 `no Robonix boot state; nothing to stop`，然后才在后面 WARNING 里
报 `265641 python3 -m internvln_navigation.provider` 还活着。

**原因**：失败的 `rbnx boot` 把 `rbnx-boot/state.json` 删了（它以为自己是这个 state 的主人），
而上一套栈其实还在跑、还占着 50051。`stop.sh` 唯一的入口是 `state.json`，没有它就当"没事可做"，
只能靠后面的 pgrep 报警——等于要人手工 kill。

**改动**：`stop.sh` 在没有 state 文件时，先按 `rbnx boot ... -f ...robonix_manifest` 找到 boot
父进程，SIGTERM（子进程随之退出），等 10 秒，仍不退再 SIGKILL；一条都没有才打印
"nothing to stop"。state 文件存在时行为不变。

**这次现场怎么清的**：两套残留的 `rbnx boot`（260675、265145）TERM 掉，子进程全部随之退出，
50051/50061/50071/50081/50091/50093 全部空出，之后重新 `bash start.sh` 正常。

**能回退吗**：能，删掉那段 `else` 分支里的扫描即可；它只影响"没有 state 文件"这一种情况。

---

## 2026-10-30 · 修 `probe_internvla_server.py`：它一直在问"没指令"

**现象**：拿它打真服务器，每个 frame 都是 `vx=+0.0000 source=awaiting_instruction
reset=True`，看起来像"模型不给动作"，其实是我们自己的客户端没被 arm。

**原因**：客户端改成"操作员下指令才发请求"之后，`config.instruction` 只剩默认文案，真正
开闸的是 `client.set_instruction()`（ROS 节点收到指令时调的就是它）。探针没调，所以永远
停在 `awaiting_instruction`。

**改动**：`scripts/probe_internvla_server.py` 建完 client 立刻 `set_instruction(args.instruction)`，
并加了注释说清为什么。改完实测：`frame 0..2` 全是 33 点轨迹，`episodes opened: 1`。

**注意**：它以前在验收文档里被当成"模型通不通"的证据，其实只证明了 socket 通、模型根本没被
问过。看它输出时记得区分。

---

## 2026-10-30 · 关掉中途清历史（`history_reset_frames: 8 → 0`）

**用户反馈**："车子容易丢失目标，理论上目标出现就会锁定；车驶过之后不知道停下来，目标跟丢了
依然前进，是不是没有处理好记忆的问题？我理解是不是会丢帧，丢失关键的历史帧导致模型给出来的
指令不准。"

### 结论：是记忆问题，而且是我们自己挖的

模型记东西只有一条路：服务端存着本 episode **从第一帧起的每一帧**，每次评估用
`np.linspace(0, episode_idx-1, 8)` 在整段里均匀抽 8 张当上下文
（`internvla_n1_agent_realworld.py:184`）。**训练时抽法一模一样**
（`internvla_n1_lerobot_dataset.py:1004`，同一个 linspace，从第 0 帧起），所以这 8 张就是
模型对场景的记忆、也是它"第一次在哪儿看见目标"的唯一记录。

官方客户端从头到尾只 reset 一次：`policy_init` 只在第一帧是 `True`
（`http_internvla_client.py:54`），之后没有任何清历史。

我们在 2026-10-29 加了一个"每 N 帧清一次"的旋钮，2026-10-30 又调成 8。现场那次
`requests: 45, memory_flushes: 5`——**每 8~10 秒失忆一次**。目标一离开画面，模型既没有
"我刚才在哪见过它"的证据（锁不住），也不知道自己已经开过去了（不停，继续往前画路）。

当时加它的理由是"跑久了抽到的多是转身前的旧画面"，那是**猜的**，而且与训练分布相反。

### 改了什么

| 文件 | 变化 |
|---|---|
| `robonix_manifest.yaml` / `config/internvln_navigation.yaml` | `history_reset_frames: 8 → 0`，注释改成"为什么必须是不清"（带训练端和官方客户端的出处） |
| `core.py` | `RuntimeConfig.history_reset_frames` 默认 8 → 0，docstring 重写 |
| `tests/test_http_client.py` | `test_the_flush_is_part_of_the_packaged_default` → `test_the_memory_is_kept_by_default`，断言默认 0 |
| `docs/ARCHITECTURE.md` | "为什么要在一条指令跑到一半时清一次历史"整段改写成"中途清历史：默认关闭"，带现场数据；第 399 行的记忆窗口小节同步 |
| `docs/DEPLOY.md` / `docs/ACCEPTANCE.md` | 同步成"不再中途清；`memory_flushes` 应保持 0" |

机制本身没删：`history_reset_frames: N` 仍然是"每 N 帧清一次"，只是默认关。

### 回退

`history_reset_frames: 8` 即回到今天之前的行为。想验证到底是不是这条：跑一趟看 status 的
`memory_flushes`——现在是 0 且不涨。

### 没改、也改不了的：目标丢了不会原地转

用户期望"看不到物体应该是原地旋转找目标"。查证结论：**模型没有这个动作**。

- 动作表只有 `STOP / ↑ / ← / → / ↓`（`internvla_n1_agent_realworld.py:61`），`STOP` 的含义是
  "任务完成"，没有"搜索"；
- 服务端那段 `look_down` 二次推理（`http_internvla_server.py:71`）是模型吐 `[5]` 时让它
  "说说看到什么"的**消歧**，不是环视；
- "找不到就转 30°"只出现在官方另一个变体（VLLN）的 prompt 里（`vlln_lerobot_dataset.py:350`），
  我们跑的 DualVLN checkpoint 没有这句。

本栈**有能力**原地转（Ranger 在 `linear.x == 0` 时就是原地转，guard 也放行），缺的是
"目标丢了"这个**信号**。要做成，得在本机加一个开放词表检测器（拿指令里的名词当 query，
每帧给出"在不在、在画面哪侧"），那是一个新组件（模型、显存、延迟），不是改配置。
先看这次记忆修正之后的实车表现，再决定要不要上。

### 验证

- `python3 -m unittest discover -s tests -q` → **322 跑，6 红**，全是 `test_set_vln_mode` 的
  环境性红（栈正在跑）；非环境红为 0。
- `python3 scripts/validate_manifest_consistency.py` → `manifest consistency OK`。
- 起栈 + 下发同样那句盆栽指令，实测：`requests=87, episode=1, frame_id=87,
  memory_flushes=0`（改之前同样的指令是 `requests=45, memory_flushes=5`，每 8 帧失忆一次）。
  清历史那条路已经不进循环了。

### 补充：同一帧、同一条指令，离线复现（证明 STOP 不是记忆改动带来的）

现场那次 87 个请求里，模型从第 1 个请求起就回 `discrete_action[0]`（STOP），连着 43 次、
30.3 秒，被 `finish_after_stops` 规则当成"到了"，计划退役、转入 idle。看起来像是"不清历史
反而让它以为到了"，所以单独验了一次：

`scripts/probe_internvla_server.py` 的同一套客户端、同一张当前相机帧、同一条指令，
`history_reset_frames: 0` 连发 14 帧（历史一直留着，没有 flush）——**14 帧全是轨迹**
（33 个点，`trajectory bearing -7.6° forward 0.300 m`），一次 STOP 都没有。

所以"不清历史 → 一直 STOP"不成立。那次 STOP 是那个时刻的画面/视角造成的（要么目标就在
跟前，要么相机刚起来画面还不对），与这次改动无关。这也是"目标丢了/到了"的判断全靠视觉的
老问题：模型只有画面一条信息源。

---

## 2026-10-30 · 修正：轨迹答案也变成地上的点（"到了不停、到处乱晃"的真因）

**用户反馈**："依旧是 S 型只是幅度小了一点，另外车子到了目的地不知道停，继续到处晃错过目标，
什么原因？"

### 根因：模型回的是 `trajectory`，而轨迹这条路根本不进伺服

现场 `/ranger/vln/status` 抓到的原文：

```
servo: {enabled: true, active: false, goal: null}
last_note: "trajectory: trajectory bearing -0.9 deg, forward 0.300 m
            over 1.20 s (arc radius 9.66 m) (held longer: chassis speed cap)"
```

一口气四个后果，全部来自"轨迹不是离散动作"这一件事：

1. **`goal: null`** —— 轨迹被换算成一个**速度**（纯追踪），`program` 里没有任何 `turn_rad` /
   `distance_m`，`goal_from_program` 自然返回 None。伺服整段没参与，走的是
   `StepController`，而 lockstep 关着时它就是**把模型给的速度原样保持**到计划过期。
   于是 S 形只从"钉错位姿"那边好了一点（上一条修正），闭环一点也没有——上一版的
   "没改的"里写着这条，这次补上。
2. **不会停** —— 一个保持住的速度没有"到了"这个状态。没有目标点就没有
   `SERVO_DONE`，只能等计划过期发 0，然后下一个答案又给一段新的前进。
3. **永远结束不了任务** —— `model_stop` 只在**离散**动作全是 0 时置位；"连续 N 次 STOP 才算
   完成"的判据对"只回轨迹"的模型永远不成立。这就是"到了目的地不知道停"的第二半。
4. **短轨迹 = 原地蹭** —— 原来只有一个 1e-6 的保护，一条 2 cm 的路径会被当成 0.02 m/s
   的**速度**保持整个时长。模型其实在说"就在这儿"，栈却在执行"往前挪两厘米"。

### 模型本身没有"找目标"这个动作

顺带把这条说清（用户问过）：`internvla_n1_agent_realworld.py:61` 的动作表只有
`STOP / ↑ / ← / → / ↓` 五个，S2 的 prompt 是"Where should you go next to stay on track?
… Please output STOP when you have successfully completed the task."——**没有搜索、没有
"目标丢了"。** 所以"原地旋转找目标"不是模型能给的，也不是官方客户端做的；目标丢了它只会继续
按画面画路。本栈有能力原地转（Ranger 在 `linear.x == 0` 时就是原地转），但**缺的是一个
"目标丢了"的信号**，这次不做，免得凭空造一个启发式。

### 改了什么

| 文件 | 变化 |
|---|---|
| `core.py` | 新增 `goal_from_waypoints(start, waypoints, lookahead_m, trajectory_stop_m)`：用**和适配器同一套规则**选出被追踪的点，把位移叠在**答话那一刻的位姿**上，朝向转到指向它；路径短于 `trajectory_stop_m` 或点串为空 → None。`PlanStore.update` 在 `goal_from_program` 为 None 且有 `waypoints` 时调用它。`DispatchState` 加 `waypoints` 字段（判断"这份计划有没有几何"必须看得见轨迹） |
| `core.py` | 新配置 `trajectory_stop_m`（默认 0.05，`_positive_float` 校验） |
| `action_adapter.py` | `trajectory_to_velocity` 加 `stop_m`：距离 ≤ 它 → 零命令 + `model_stop=True` + note 写 `nothing to drive, holding (arrived)`；`ActionStep` 加 `model_stop` 字段；三条 note 都补 `N points, extent X.XX m` |
| `action_adapter.py` | `parse_plan` 把 `step.model_stop` 带进计划，并把 `config.trajectory_stop_m` 传下去 |
| `ros_node.py` | `wants_geometry` 加上 `bool(state.waypoints)`；闸门条件改成 `pose is None and wants_geometry`（**没有位姿才 hold**；有位姿但目标为空，是"就在这儿"，照常交给分段执行器保持）。`PlanStore(..., lookahead_m=, trajectory_stop_m=)` |
| `robonix_manifest.yaml` / `config/internvln_navigation.yaml` | 加 `trajectory_stop_m: 0.05` 和原因 |
| 测试 | `test_core`：`goal_from_waypoints` 三条 + 存储层一条；`test_action_adapter`："短轨迹=就在这儿"替换原来的"短轨迹按最后一点走"（并新增一条 6→12 cm 的仍有速度，证明阈值不是死区）+ 计划层 `model_stop`；`test_static_contract`：新增"轨迹答案也变成目标"和"轨迹没位姿也不许盲走" |

### 回退

全挂在 `servo_enabled` 上：关掉就是今天之前的行为（速度保持 + 到期停）。只想关掉"短轨迹算
STOP"：`trajectory_stop_m: 0.0`（则退化成 1e-6 的老保护）。想彻底删这条改动：删
`PlanStore.update` 里 `if goal is None and plan.waypoints:` 那一段即可，伺服回到只认离散动作。

### 没改的 / 仍然会犯的

- **目标丢了仍然会往前走**：模型没有"丢了"这个输出，我们也不去猜。现在能保证的是"走到点上
  就停、够近就当到了"，不是"看不见就停"。
- **S 形只对轨迹这一段解掉**：轨迹现在 50 Hz 闭环追一个固定点；但答案是 1-3 s 一次，
  点只在前视 0.30 m，走完到下一个答案之间还是会有停顿（这是 1 Hz 闭环 + 短前视的固有粒度，
  和上一条"一顿一顿"同源，不是这次的回归）。
- `max_chunk_actions` 仍是 1；`lookahead_m` 仍是 0.30；没有避障；没有搜索动作。

### 验证

- `python3 -m unittest discover -s tests -q` → **322 跑，6 红**，全是 `test_set_vln_mode` 的
  环境性红（manifest 停在 `live`，现场有 `ranger_base_node` 在跑）；非环境红为 0。
- `python3 scripts/validate_manifest_consistency.py` → `manifest consistency OK`。
- **没起栈、没动 `mode`**（manifest 是 `mode: live`）。下次起栈后先看 `last_note`：
  `trajectory: ... N points, extent X.XX m`，以及 `status.servo.goal` 不再是 `null`。

---


**用户反馈**："让它行驶到最近的白色盆栽，它会走 S 型接近目的地，但是会走过，然后再更远处的
白色盆栽处停下。"

### 根因：上一版把目标钉在"拍帧时的位姿"上，官方钉在"答话时的位姿"上

官方 `InternNav/scripts/realworld/http_internvla_client.py:313`：

```python
homo_goal = self.homo_odom.copy()      # ← 答话这一刻的 odom
```

`incremental_change_goal` 是**从车现在的位置**往上叠 0.25 m / ±15°，不是从拍帧时的位姿。
上一版按"拍帧位姿"算，理由是"手眼对齐"，实际两个后果都落在地上：

1. **一顿一顿**：一个来回约 1 s，车在这 1 s 里正好开 0.25 m = 一整步。按拍帧位姿算出来的
   目标就落在车轱辘底下 → 伺服判"到了" → 发 0 → 等下一帧。仿真实测 **0.10 m/s、14 % 的
   时间停着**。
2. **S 形**：目标的**朝向**是 1 s 前的绝对朝向。车在这 1 s 里转过的角度，伺服会一点点把它
   纠回去；纠过头，下一帧模型又说往另一边，再纠回来——地上看就是左右摆。

改成读当前位姿后：**0.20 m/s、0 % 停顿**；纯"前进"流里偏航误差恒为 0，不会再往旧朝向纠。

**"走过、停到更远的盆栽"这一条这次修不掉**：模型看的是约 1 s 前的图，它说 STOP 时车已经又
走了约 25 cm。这是 1 Hz 闭环 + 25 cm/步的固有粒度，官方同样如此（它 `max_v` 0.6，只会更冲）。
要收小只能降 `max_vx`。

### 改了什么

| 文件 | 变化 |
|---|---|
| `worker.py` | 新增 `pose_source` 回调：答案回来时现读一次位姿当 `reference_pose`；没给就退回 `frame.pose`（无 odom 的调用方） |
| `ros_node.py` | 新增 `_latest_pose(now=None)`（一处定义，拍帧与答话共用）；`InferenceWorker(..., pose_source=self._latest_pose)`；`_on_image` / `_dispatch` 改调它 |
| `core.py` | **只改注释**（`servo_enabled` 那段、`PlanStore.update` 那段），逻辑一行没动 |
| `robonix_manifest.yaml` / `config/internvln_navigation.yaml` | 注释改成"从答话那一刻量起"，并写明为什么不是拍帧位姿 |
| `test_static_contract.py` | `test_the_frame_carries_the_pose_it_was_taken_in` 收窄成只验"帧上带位姿"；新增 `test_the_worker_measures_from_the_pose_the_answer_lands_on`；`_dispatch` 的位姿断言换成 `_latest_pose(now=now)` |

帧上**照样带**拍帧时的位姿：那是"模型当时在看什么"的记录，也是将来轨迹类答案换算世界坐标
要用的锚（官方对轨迹用的就是帧时刻的 `odom_infer`，第 160 行）。

### 回退

代码层面：删掉 `ros_node.py` 里 `pose_source=self._latest_pose` 这一行，worker 就退回用
`frame.pose`（上一版的行为）。配置层面没有开关——这不是"要不要"的问题，是钉错了位置。

### 没改的

- **轨迹答案仍然不进伺服**：`state.goal` 为 None → 退回 `StepController`，而 lockstep 关着时
  它把模型给的速度**原样保持**到计划过期（最多 1.5 s）。下次若仍看到"冲过头"，先看
  `/ranger/vln/status` 的 `last_note` 是 `discrete_action[...]` 还是 `trajectory: ...`；
  是后者的话要单独修（官方是用帧时刻 odom 把轨迹搬到世界系再交给 MPC）。
  **> 已被上面那条"轨迹答案也变成地上的点"取代（同日）。**
- `max_chunk_actions` 仍是 1；纯旋转阈值没动；没有避障。

### 验证

- `python3 -m unittest discover -s tests -q` → **317 跑，6 红**，全是 `test_set_vln_mode` 的
  环境性红（manifest 停在 `live`，且现场有 `ranger_base_node` 在跑）；非环境红为 0。
- 离线仿真（50 Hz tick / 1 s 一个答案 / 一节底盘惯性）：

  | 目标怎么量 | 平均速度 | 停顿占比 |
  |---|---|---|
  | 拍帧时的位姿（上一版） | 0.100 m/s | 14 % |
  | 答话时的位姿（现在） | 0.200 m/s | 0 % |

---

## 2026-10-30 · 指令→底盘：从"停—想—走"改成连续伺服（`servo_enabled`）

**用户要求**："车前进是一顿一顿的，每拍一帧请求拿到结果之后执行完了，再拿帧请求，目的是为了
手脑对齐，怕失真；按官方对 internvln 的描述把过程改一下，再保证手眼对齐的同时尽可能提高
执行效率，让过程丝滑一点。"

### 判断：不是模型的问题，是**循环的形状**问题

"手眼对齐"这件事，官方不是靠**车停下来**实现的，是靠**位姿跟着帧走**实现的。官方客户端
`InternNav/scripts/realworld/http_internvla_client.py` 是两个线程：

- `planning_thread`（第 106 行起）：等新帧 → 在 `manager.odom_queue` 里挑**时间上离这一帧
  最近**的那条 odom（第 129-136 行）→ 阻塞请求模型 → 回来后只做一件事：把目标位姿往前挪
  （`incremental_change_goal`，第 310 行）/ 更新 MPC 参考轨迹（用的是**帧时刻的 odom**，
  第 160 行）；
- `control_thread`（第 73 行起）：10 Hz 独立循环，拿最新 odom 对当前目标跑 PID
  （`controllers.py`：`Kp_trans=2.0, Kp_yaw=1.5, max_v=0.6, max_w=0.5`），**从不等待推理**；
- 两个线程之间只有一个共享的目标位姿。推理 1 s 还是 3 s 都一样：车一直在动，新答案只是把
  目标点挪个位置。

本栈原来把**请求放进了执行回路**：拍图 → 请求 → 等答案 → 按时间执行一段 → 停稳 → 再拍图。
答案对不对不知道，但车一定是停的，所以"一顿一顿"是这个结构的必然结果，不是调参能解决的。

### 改了什么

| 文件 | 变化 |
|---|---|
| `odometry.py` | 新增 `advance_pose`（= 官方 `incremental_change_goal` 的算术：`1` 沿目标朝向 +0.25 m，`2/3` ±15°，按顺序）+ `servo_command`（官方 PID 的比例项：平移误差投影到车头方向、偏航误差取最近角；负速度钳到 0，只前进不后退） |
| `core.py` `PlanStore` | `update(plan, reference_pose=...)` 里一次算出世界系 `goal`（帧位姿 ⊕ 动作序列），`DispatchState` 带上 `goal` / `reference_pose` |
| `core.py` `EncodedFrame` / `Mailbox` | 帧上新增 `pose` 字段：**拍图那一刻的位姿**，这是手眼对齐的锚点 |
| `core.py` `RuntimeConfig` | `lockstep_enabled` 默认 **false**；新增 `servo_enabled`、`servo_kp_trans`、`servo_kp_yaw`、`servo_position_tolerance_m`、`servo_yaw_tolerance_deg` |
| `ros_node.py` | `_on_image` 把位姿钉进帧（在解码/编码之前取，尽量贴近曝光）；50 Hz dispatch 改成伺服；worker 只有在**servo 关掉且 lockstep 开着**时才装闸；status 新增 `servo` 段（是否在追、追到哪、差多少） |
| `worker.py` | 把 `frame.pose` 作为 `reference_pose` 交给 PlanStore |
| `robonix_manifest.yaml` / `config/internvln_navigation.yaml` | `lockstep_enabled: false` + 5 个 servo 键 |
| `scripts/validate_manifest_consistency.py` | 打印 servo 的那行说明；锁着 lockstep 的旧 NOTE 改成"servo 开时 lockstep 只给没有几何的计划计时" |
| `scripts/watch_vln_chain.py` | 新增 `servo:` 一行（`chasing (x, y, yaw) err=..m dyaw=..deg` / `goal reached` / `idle`），odometry 那行跟着 servo 解释"停住"还是"退回计时" |
| 测试 | `test_odometry` 增 13 条（advance_pose / servo_command 的方向、限幅、不回退、容差、π 缝合）；`test_core` 增 `ServoConfigTests` + `PlanGoalTests`；`test_worker` 增"帧位姿传到 store"；`test_static_contract` 增 4 条（帧带位姿、dispatch 伺服、不盲走、闸门只跟 lockstep+servo 有关） |

### 手眼对齐为什么没丢

> **已被上面那条修正取代。** 这里说的"从拍帧位姿扣掉推理期间走过的距离"没有错，只是不够：
> 一个来回正好等于一整步，扣完目标就落在脚下。现在改成从**答话那一刻**的位姿量起
> （官方 `homo_goal = self.homo_odom.copy()`）。

答案永远相对**拍这张图时车所在的位姿**理解。推理期间车走了 0.12 m，这 0.12 m 就从这个目标里
扣掉，不重不漏 —— 这正是"靠位姿对齐"而不是"靠停车对齐"。附带好处：目标不会再随车漂移
（如果每 tick 拿"当前位姿 + 0.25 m"重算，就变成追一个永远追不上的点）。

### 只有一件事它拒绝做

计划里写了距离/角度，但帧上没有位姿（odom 没开、断了，或拍图那一刻没有样本）→ **停住不动**，
`state=servo_wait_odom`，而不是退回"按时间盲走"。所以 `servo_enabled: true` 时 odom 半路
死掉，车是停，不是瞎跑。两个开关刻意解耦：`odom_feedback_enabled` 只管"一段算不算走完"，
`servo_enabled` 只管"计划是不是一个目标位姿"，任一开着都会订阅 `/odom`
（否则 `servo: true` + `odom_feedback: false` 会因为拿不到位姿而永远停着）。

### 怎么回退（不动代码）

- **回到分段执行**：`servo_enabled: false`；
- **连走走停停也退掉**：`servo_enabled: false` + `lockstep_enabled: false`；
- **回到 2026-10-30 上午的行为**：`servo_enabled: false` + `lockstep_enabled: true`
  （就是那次"odom 闭环分段 + 走走停停"的组合）。
- 都需要**重启栈**才生效（配置是启动时读的）。

### 没改的（明确留给下一步）

- `max_chunk_actions` 仍是 **1**。伺服解决了"走一步停一下"，不需要靠放大动作块来提速：
  每 1 s 一个新目标就够连续了。调 4 仍是下一件事，风险和以前一样（一次决策盲走更远）。
- 轨迹计划仍然走纯追踪 + 定时段（没有几何 → 没有目标位姿）；官方是 MPC 追整条世界系轨迹。
- 纯旋转阈值（≈10.7° 就原地转）没动。
- 没有避障。

### 验证

- `python3 -m unittest discover -s tests -q` → **316 跑，5 红**，全是 `test_set_vln_mode`
  的环境性红（manifest 停在 `live`），与本轮无关；本轮改动前后非环境红为 0。
- `python3 scripts/validate_manifest_consistency.py` → OK。
- 离线仿真（50 Hz tick / 1 s 一个答案 / 一节底盘惯性）：持续 `[1,1,1,1]` 时 `vx` 稳定在
  0.25 m/s 上限、**中途无归零点**；持续 `[2,2,2,2]` 时 `wz` 稳定在 17.2°/s。

---

## 2026-10-30 · 指令→底盘：改成 odom 闭环执行 + 记忆窗口收到 8 帧

**用户要求**："针对这些问题……核心两个：一是请求抽帧有问题，引入过多无用帧；二是模型给的
指令没有正确应用在车上，和 internvln 官方适配有瑕疵。你从这两个角度排查，看有没有值得完善
的地方。" 随后："先按你的改起来吧，注意留痕方便回退。"

### 排查结论（对照官方源码）

官方客户端 `InternNav/scripts/realworld/http_internvla_client.py`：

- 一次回答里的**整块**动作都执行完才取下一帧（`incremental_change_goal` 把 4 条动作累加成
  一个**世界系目标位姿**，`1` = 沿目标朝向 +0.25 m，`2/3` = 目标朝向 ±15°）；
- 执行是**闭环**：`control_thread` 10 Hz 拿 `/odom` 跑 PID（`Kp_trans=2.0, Kp_yaw=1.5`）
  追那个目标位姿，轨迹走 MPC；**从来不是"按时间开环走一段"**；
- 推理在后台线程跑，**车不停**，新答案到了才换目标；
- 服务端 `--plan_step_gap 4` 的含义是"每 5 帧才推一次 S2"，其余帧只入历史
  （`step_no_infer`），官方 notebook 原话：推太快会让记忆间隔过密、产生 sim-to-real gap。

我们原来的两处偏差：

1. **执行开环**：`discrete_to_velocity` 算出"1.0 s 内走完 0.25 m / 15°"的速度，然后**按时间**
   一直发到点。实测标称 0.25 m 只走 ~0.2 m（见本文件 2026-10-22 那条），且误差随地面/坡道/
   上一段滑行变化 —— 逐步累积就是"停在目标前面一截"、"开过头还在走"、"转了 15° 其实没转到"。
2. **记忆窗口从不触发**：`history_reset_frames: 20`，而一个 episode 只有 5~15 个决策，
   等于没开；服务端每帧都用 `np.linspace(0, episode_idx-1, 8)` 从**整段**里抽 8 帧，
   于是模型看到的永远是开头那几帧（转向之前、目标还没进视野的画面）。

### 改了什么

| 文件 | 变化 |
|---|---|
| `internvln_navigation/odometry.py`（新） | `PoseBuffer`（最新 `/odom` + 超时判定）、`yaw_from_quaternion`（用官方同款公式）、`turn_reached` / `distance_reached`（带方向的"够了没有"） |
| `core.py` `StepController` | 每段可以按 odom **提前结束**：转够角度 / 走够距离就换下一段；没有 odom 或该段没有目标（STOP、相机动作、轨迹）时**仍然按标称时长**；另外 `odom_segment_timeout_factor`（默认 2.0）兜底，卡住的车不会把一段无限挂着 |
| `core.py` `ProgramSegment` / `RuntimeConfig` | 段上新增 `turn_rad` / `distance_m`；新增 6 个配置键（见下） |
| `action_adapter.py` | `discrete_to_velocity` 把动作的几何带出来（`1`→0.25 m，`2/3`→±15°）；`parse_plan` 写进 program |
| `core.py` `PlanStore` | 看门狗寿命跟着放长：带目标的段可以跑到 `时间因子 × 标称时长`，否则 1.5 s 的 `plan_hold_s` 会抢在 stepper 之前把段砍掉（车"莫名其妙停短了"且没有日志） |
| `ros_node.py` | 订阅 `/odom`（dry-run 也订，用来回答"到底有没有 odom"）；每 tick 把新鲜位姿交给 stepper；status 增加 `odom` 段和 `step.goal/feedback/segment` |
| `robonix_manifest.yaml` / `config/internvln_navigation.yaml` | 新增 6 个 odom 键；`history_reset_frames: 20 → 8` |
| `scripts/watch_vln_chain.py` | 显示 `segment=x/y goal=... feedback=odom|timer` 和一行 odometry 状态 |
| 测试 | 新增 `tests/test_odometry.py`（12 条）；`test_core` 增 9 条闭环/配置用例；`test_action_adapter` 增 3 条；`test_static_contract` 的 dispatch 断言同步 |

新配置键：`odom_feedback_enabled: true`、`odom_topic: /odom`、`odom_timeout_s: 0.5`、
`odom_turn_tolerance_deg: 1.5`、`odom_distance_tolerance_m: 0.02`、
`odom_segment_timeout_factor: 2.0`。

### 怎么回退（一处开关，不动代码）

- **要回到"纯计时"的旧行为**：`robonix_manifest.yaml` 里 `odom_feedback_enabled: false`
  → 每段仍然按标称时长结束，`odometry.py` 与相关订阅都不参与，行为与 2026-10-22 版一致。
- **要回到旧记忆窗口**：`history_reset_frames: 20`（或 `0` 彻底关掉清理）。
- 两个键都只影响启动时读到的值：**重启栈才生效**；运行中的栈可以先用
  `scripts/set_vln_instruction.sh` 换指令做对照，但键值改不了。

### 没改的（明确留给下一步，别当成已修）

- `max_chunk_actions` 仍是 **1**：官方执行整块 4 条，我们只执行 1 条就重新问。闭环做好之前
  这么改等于盲走 1 m；**闭环上车验过之后**再把它调到 4（注释里已写明这是下一个实验）。
- 轨迹仍是纯追踪、每段只跑 0.3~0.5 s；官方的 MPC 是世界系连续追整条轨迹。
- 纯旋转阈值（`lookahead 0.3 / R_min 0.81` ≈ 10.7°）没动：偏一点就原地转是"视野丢失"的
  另一半原因，但它和底盘 `min_turn_radius` 绑定，要单独评估。
- `safety_gate`（深度急停）与"避障"仍然没有：撞玻璃/行人那一类问题不在这次改动范围内。

验证：`python3 -m unittest discover -s tests -q` → 288 跑，仅 6 条 `test_set_vln_mode`
是环境性红（manifest 是 `live` 且 `ranger_base_node` 在跑，与本轮无关）；
`python3 scripts/validate_manifest_consistency.py` → OK（55 个配置键）。

---

## 2026-10-30 · 上服务器核实：指令是生效的，原地转来自模型自己的转向动作块

**用户要求**："为什么会原地旋转……你给我一个方案怎么解决。"

### 核实结果（10.0.105.11，cwd `/home/xfusion/hhy/InternNav`）

- 跑的进程：`python scripts/realworld/http_internvla_server.py --device auto --model_path … --num_history 8 --plan_step_gap 4`；
- `http_internvla_server.py:45` 是
  `instruction = data.get('instruction', None) or "Turn around and walk out of this office. …"`
  ——**读请求里的指令**，那句只是兜底；`depth` 可选（缺了填全零）。
  **上一轮"服务端不读指令"的判断作废**，依据是本机镜像里那份旧副本；
- 那台机器上没有 `scripts/realworld/server.py`，也没有 `/task/start`，即产品版不存在于此，无需迁移。

### 原地旋转的真正来源：服务器自己存了每帧的模型原始输出

`test_data/<时间戳>/llm_output_NNNN.txt` 就是 System 2 每帧的原始回答。2026-10-29 18:42–18:44
那几次运行的记录：

```
→→→→ →→→→ →→→→ →→→→ →→→→ →→→→ →→→ →→→ →→→ →→→ →→ →→ ←←←← ←←←←    (一条运行，15 帧)
←←←← ←← ←←←← …                                                       (另一条)
```

**模型自己连续十几帧吐"转 15°"的动作块**，一次运行向右转到 ~180° 再反着转回来。本栈只是照做
（`max_chunk_actions=1`，每帧执行其中一步），所以看起来是"原地慢慢转、停不下来"。
同一帧换指令复测（`vln_query.py`/curl，`--noproxy`）：

| 指令 | 回答 |
|---|---|
| "Turn left." | `discrete_action: [2,2,2,2]` |
| "Go to the light wooden staircase next to the white planter and stop there." | `[3,3,3,3]` |
| "Move forward." | 轨迹（像素目标 207,123） |
| "Walk straight ahead down the hallway and stop." | 轨迹 |
| "Go to the white planter and stop." | 轨迹 |

结论：指令通道是通的，模型听指令；**它给出的转向是因为目标不在视野/找不到**（"light wooden
staircase"那条要它自己转着找）。选一个画面里看得见的目标，模型就给轨迹而不是转向。

### 文档修正

`docs/DEPLOY.md` 4.2 的"服务端可能不读指令"警告改成核实过的结论；`docs/ARCHITECTURE.md`
对应那句同步。本机镜像那处改动（`data.get`）与线上行为一致，保留。

---

## 2026-10-29 · 一条指令跑久了自动清一次模型历史（`history_reset_frames`）

**用户要求**："优化一下记忆清空的机制（我担心的问题）"——担心的是"原地乱转"是因为
模型端记忆没清、把旧图像当成了当前证据。

### 服务端到底怎么用历史

`internvla_n1_agent_realworld.py` 里，服务端把本 episode **从第一帧起的每一帧**都存着，
每次评估用 `np.linspace(0, episode_idx - 1, 8)` 在一整段里**均匀抽 8 帧**。
所以：

- 第 10 帧时，8 个样本覆盖第 0~9 帧，全是刚看到的；
- 第 200 帧时，8 个样本从第 0 帧一路散到第 199 帧，**每帧间隔约 28 步**——其中大量是
  机器人转身之前、还在看别的方向时的画面。模型据此判断"到没到"，自然可能给出和眼前
  画面矛盾的动作（原地转就是一个典型表现）。

### 改法

新旋钮 `history_reset_frames`（默认 **20**，0 关闭）：同一条指令的发帧计数到 20，下一帧
带 `reset: true` 但**指令一个字不改**。服务端 `agent.reset()` 清空帧历史、`episode_idx`
归零，接下来 8 个样本全部来自新窗口。

- 这不是新 episode：`episode` 不加一，单独的 `memory_flushes` 计数；
- 状态行 `last_note` 会带 `memory flushed`，免得值班的人看到 reset 以为指令被撤了；
- 20 是取舍：8 帧一清等于每 8 步失忆，20 帧约两倍于采样窗口，窗口被锁在近期又能留一段滑动历史。

改动：`core.py`（旋钮 + `from_mapping` 解析）、`http_client.py`（`_evaluate` 里按发帧数
触发、`snapshot()` 增 `memory_flushes`、`episode` 的注释改成"指令 episode 数"）、
两份 yaml、`tests/test_http_client.py` 加 4 个用例（到点清、清时不换指令不换 episode、
0 关闭、打包默认值 ≥ 8）、`docs/ARCHITECTURE.md`、`docs/ACCEPTANCE.md`。

### 另：服务端写死的那句指令

`http_internvla_server.py:40` 的 `instruction = "Turn around and …"` 是**写死的字面量**，
请求里的 `instruction` 字段它根本不读。这行**不能单独注释掉**：下面两处 `agent.step(...)`
会直接 `NameError`。要改就改成
`instruction = data.get('instruction') or "Turn around and …"`，在**服务端**改，本仓不用动。
排查与验证命令见 `docs/DEPLOY.md`（"写死的字面量"那一节）。

**同日补**：本机镜像已按上面改好（`/mnt/ssd/wxf/jiaofu/internVLN/InternNav/scripts/realworld/http_internvla_server.py:44`，
并加了一行 `print(f"instruction: {instruction}")`），`py_compile` 通过。**镜像不等于服务器**：
那台 GPU 机本仓不连，搬运/重启命令写在 `docs/DEPLOY.md` 4.2。兜底的默认句保留原样，
官方 Go2 客户端不发 instruction 字段就靠它。

---

## 2026-10-29 · 轨迹换算改成标准纯追踪；动作块回到"一次一步"

**用户要求**："按你说的改，然后慢系统的指令参照狗的逻辑，不需要全部执行"。

### 一、快系统：`wz = 方位角 / 时长` → 标准纯追踪

**问题**：旧式子的隐含半径是 `前进量 / 方位角`。只有当选中点正好在两倍前进量远的地方，
它才等于模型的半径；**正常情况下（点就在一个前视距离处）算出来是模型的两倍**——车走的弧比
模型画的平一半，靠每帧重拍才勉强追回来。这跟"转弯不灵敏"是同一个方向。

**证据**（用官方 `traj_to_actions` 造一条半径 2.86 m 的 32 步弧，取不同距离的点）：

| 取点距离 | 方位角 | 旧式子半径 | 纯追踪半径 |
|---|---|---|---|
| 0.40 m | 3.0° | 5.73 m | 3.82 m |
| 0.70 m | 6.0° | 2.86 m | 3.34 m |
| 1.00 m | 9.0° | 1.91 m | 3.18 m |
| 1.29 m | 12.0° | 1.43 m | 3.10 m |

真值 2.86 m。旧式子随取点远近从 5.73 甩到 1.43（4 倍），这本身就说明它不是曲率估计；
纯追踪稳定贴着真值，取样越远越准。

**改法**：`wz = 2 · vx · sin(α) / L`（L = 到选中点的实际距离），隐含半径 `L / (2 sinα)`
——过该点、与当前朝向相切的圆。原地转那一支**不动**（那里没有弧可谈，目标就是转到位对的角度）。

**连带**：原地转的分界点从 ±20.3°（`atan2`）变成 **±10.7°**（`asin(0.30 / (2 × 0.81))`）。
弯更"真"了，所以更多路径现在会正确地判成"底盘转不过去、就地转"。

### 二、慢系统：`max_chunk_actions` 4 → 1

**用户要求**"参照狗的逻辑，不需要全部执行"。狗确实整条跑完，但它**每一步都是闭环**的
（`_chassis.move(forward_m=…)` 阻塞到走到位才返回；官方客户端是 PID 追里程计目标位姿）。
本栈每一步是**开环盲走、没有避障**，4 步就是一次决策盲走 1 m。所以：

- 默认 **1**：一步 0.25 m 或 15° → 停车 0.5 s → 拍新帧 → 重新决策；
- 顺序仍然是模型的，截断只从尾部截；
- 机制（`ProgramSegment` / `StepController` 逐段驱动 / 逐段钳位）**全部保留**，
  想要整块执行就把这个值调回 4。

狗那边"每条动作的物理含义"（1 = 0.25 m、2 = +15°、3 = −15°、0/5/9 跳过）我们保持一致。

### 改了哪些文件

| 文件 | 变化 |
|---|---|
| `action_adapter.py` | `trajectory_to_velocity` 的弧分支改纯追踪（`2·vx·sinα/L`）；原地转分支用 `方位角/时长` 单独算、单独缩放；模块级 docstring 与 `executable_actions` 的说明改成"默认一步、机制保留" |
| `core.py` | `max_chunk_actions` 默认 4 → **1**；`max_chunk_actions` / `lookahead_m` 的注释改写 |
| manifest / 包内 yaml | `max_chunk_actions: 1`；`lookahead_m` 注释改成纯追踪口径 |
| `validate_manifest_consistency.py` | 原地转分界点改用 `asin(L / 2R)`（10.7°）；"最多 N 步"的措辞改成"其余等下一帧" |
| 测试 | 新增：`test_the_commanded_radius_is_the_models_radius_not_double_it`（圆圈构造，半径必须是 2.86 而不是 5.73）、`test_the_default_is_one_step_per_decision`、`test_the_cap_can_be_raised_to_drive_whole_chunks`、`test_a_raised_cap_keeps_the_order_and_the_geometry`；改写：所有轨迹测试改成断言**几何半径** `L/(2 sinα)`（不再断言实现公式）、20° 那条改成原地转（真实半径 0.62 m < 0.81 m）、`[0,1,1,1]` 的日志串改成 `1 step(s)` |
| 文档 | `ARCHITECTURE.md` 轨迹公式与分界点、动作块章节；`ACCEPTANCE.md` 的两条"现场须知"和"左转"验收判据 |

### 没有改的

- 动作码含义、0.25 m / 15° 的物理尺寸、坐标（车体系、米、无 yaw）、`lookahead_m = 0.30`；
- 等比例缩放 + 按几何拉长时长（2026-10-29 早些时候那轮）；
- 狗那边**故意没学**的两条：不照抄 `trajectory_to_command` 的"方位角 > 15° 就原地转、
  否则直走"（它自己的 docstring 说那是 one-step fallback），也不引入 MPC（本栈 lockstep
  跑完就停，MPC 的视野播不完）。

### 验证

- 单测 **258 跑，5 红全是 `test_set_vln_mode`**（manifest 停在 `live`、现场还有
  `ranger_base_node` 在跑，与本轮无关）；
- `python3 scripts/validate_manifest_consistency.py` → OK，打印
  `最多 1 步` / `past ±10.7 deg it pivots`；
- **没有跑 live，也没有连远程服务器。**

**现场第一次跑要注意**：弯上转向比以前快约一倍，原地转的分界点从 20.3° 降到 10.7°，
**手放急停上**。

---

## 2026-10-29 · 按官方本体实现执行动作块：整条跑完、几何决定时长

> **同日后续**：用户随后要求"参照狗的逻辑，不需要全部执行"，所以这一条里的
> "整条动作块全跑"**已被上面那条取代**（`max_chunk_actions` 回到 1）。
> 本条里**仍然有效**的部分是：按几何反算时长、限速只拉长不改位移、
> 等比例缩放（不分轴钳）、0/5/9 的跳过语义、以及对着官方源码做的符号/量纲/坐标系核对。

**用户要求**："模型给的指令确实有问题……第二个是模型给的指令转成 Twist 格式有问题，你研究一下
狗的本体项目的指令转换逻辑（`syswonder/skill-internvla-navigation-rbnx`），让车在执行模型指令
的时候能够仿造该本体项目的程度，不要过分执行也不要无效执行（幅度太大太小都不行）；第三，
确认快慢系统两种模式下转 Twist 的逻辑是不是有问题，模型给的指令要严格验证官网描述去解读"。

**核对结果**（源码 + 本地官方仓 `/mnt/ssd/wxf/jiaofu/internVLN/InternNav`）：

- 动作码语义**一致**：`idx2actions = {0:STOP, 1:↑, 2:←, 3:→, 5:↓}`
  （`internvla_n1_lerobot_dataset.py:950`），训练标签由
  `trajectory_to_discrete_actions_close_to_goal` 生成——`delta_yaw > 0 → 2`，即 **2 是左转**；
  我们的 `wz > 0 = 左` 与之一致，没有符号错误。
- 轨迹坐标系**一致**：官方 `trajectory_tovw` 直接把 `trajectory[-1]` 的 `[x, y, yaw]`
  当本体坐标用；`http_internvla_client.py` 用拍图那一刻的里程计位姿把点转到世界系再喂 MPC，
  并且**跳过前 3 个点**。走走停停让"拍图位姿"≈"执行位姿"，这一步等价成立。
- **两处真的不一致**（本轮修掉）：
  1. **动作块只执行了第一个元素**。参考仓是
     `for command in commands: _chassis.move(**command)`，官方客户端 `incremental_change_goal`
     也是逐个把 0.25 m / 15° 加到目标位姿上——`[1,1,1,1]` 是 **1 m**。我们只走 0.25 m，
     每次决策只兑现 1/4，这是"幅度太小"的来源。
  2. **限速是分轴钳的**：vx 被砍到上限、wz 原样，弧变紧（前进少了、转角没少）。
     参考仓的底盘 move 是**闭环位移**，走够 0.25 m / 15° 才返回。

**改了什么**（只动 `skill-internvln-navigation-rbnx`）：

| 文件 | 变化 |
|---|---|
| `core.py` | 新增 `ProgramSegment(command, duration_s)`；`InferencePlan.program` / `DispatchState.program`；`StepController` 从"一步一个时长"改成**按程序逐段驱动**（`segments` 属性给出当前段数）；`PlanStore` 对程序逐段钳位，并让**带程序的计划**寿命 = `max(plan_hold_s, 程序总时长)`（看门狗不能砍断已经交出来的动作块）；新配置 `max_chunk_actions`（默认 4） |
| `action_adapter.py` | `ActionStep` 新增 `duration_s`；新增 `_scale_to_limits`（等比例缩放）与 `executable_actions()`（整条动作块，按顺序，可截断到 `max_chunk_actions`）；`discrete_to_velocity` 的时长由几何反算（`t = 位移 / 实际速度`），限速只拉长时长、不改位移；`trajectory_to_velocity` 改为等比例缩放 + 时长同比例拉长 |
| `ros_node.py` | `moving` 改为看**整个程序**（否则动作块的尾巴还在走就开闸）；status 的 `step` 增加 `segments` |
| manifest / 包内 yaml | 新键 `max_chunk_actions: 4`（一致性检查 47 → 48 键）；注释改成"时长跟着几何走" |
| `validate_manifest_consistency.py` | 打印实际的步长/时长/位移，标出"限速时拉长而不是缩短"；动作块上限；轨迹弧超限时的处理改为等比例 |
| `watch_vln_chain.py` | status 行增加 `segments=` |
| 测试 | 新增：整条动作块（3 段/4 段/超长截断）、限速拉长时长、等比例弧、PlanStore 程序寿命与逐段钳位、StepController 分段执行/整块后才开闸/空程序仍走一步/中途换版本；改写的断言：日志串改成 `discrete_action[0, 1, 1, 1] -> 3 step(s)`、`clipped` 改成 `held longer` |

**没改的（以及为什么）**：轨迹那条路仍是"追 lookahead 点的弧"，没有照抄参考仓的
"方位角超 15° 就原地转、否则直走"——因为那个仓自己在 docstring 里写着它是
"a one-step closed-loop fallback, not official MPC trajectory tracking"；官方客户端走的是
MPC 跟踪同一条路径，弧线比"先转后走"更接近它。Ranger 的 0.81 m 最小转弯半径仍由
`guard`/转原地分支处理。

**"掉个头完美避开"怎么看**：分两层说，别再一口咬定是模型的错。

- **换算没把方向搞反**：符号、量纲、坐标系三样对着官方源码核过（`idx2actions`
  2=← 且正 yaw 即左转；轨迹点是图像时刻的车体系，官方客户端也是这么解的）。
  单步最大转角 15°，字面意义上的"掉头"得连续 12 次同向转，一步做不到。
- **但"转多少 / 走多远"的比例原来是错的，而且这一条足以推出掉头**：旧代码只执行
  动作块的**头一个**、却把转向**整份**执行，于是前进被砍成 1/4、转向一点没少 →
  每走 1 m 转的角度是模型本意的 **4 倍**。闭环里这就等于"原地多转、少往前走"，
  几轮下来目标物从正前方滑到侧面、再到背后，看起来就是"贴脸掉头避开"。
  这正是本轮修掉的两处，所以下次上场**先看这个是否消失**。
- 修完还在掉头，才轮到"模型判断"那层（指令无可达目标、画面里目标不明显）。
  定位办法：现场同一张图对比多条指令的输出（`internVLN/vln_query.py --batch`），
  看它是"一直同向转"还是"来回犹豫"。

**验证**：单测 **256 跑，6 红全是 `test_set_vln_mode`**（manifest 停在 `live`，
且现场还有 `ranger_base_node` 在跑，切回 dry-run / 停栈即绿，与本轮无关）；
`python3 scripts/validate_manifest_consistency.py` → OK。**没有跑 live，也没有连远程服务器。**

---

## 2026-10-28 · 走走停停（lockstep）：`action_step_s` 与 `plan_hold_s` 拆开，执行完一步再拍照

**用户要求**："转向不灵敏……有的时候转弯方向一大，目的地就消失在视野里了"；排查后确认是
**手脑不同步**（执行的是很久以前看到的画面），先按方案 1（里程计闭环）讨论，最后定为
**方案 2：执行完当前动作 → 停车 → 拍新帧 → 问模型 → 再走**。

**实测证据**（`rbnx-boot/logs/internvln_navigation.log`，493 份 live 计划）：

- 模型回答间隔 **p50 0.778 s / p90 0.915 s**，98.3% < 1.5 s；
- 而 `plan_hold_s`（当时**既是**看门狗**又是**速度除数）= 1.5 s，所以每份计划的动作只被执行了
  约 **52%**（15° 只转 ~7.8°）；
- 更糟的是它**没有闭环**：命令最多落后画面 2.3 s（等待 + 推理 + 执行），模型偶尔重复一个
  答案时，同一个动作被连续推 1.5 s，**转过头**，目标直接出画面。

**改了什么**（只动 `skill-internvln-navigation-rbnx`）：

| 文件 | 变化 |
|---|---|
| `core.py` | 新配置 `lockstep_enabled`（默认 **true**）、`action_step_s`（默认 **1.0**）、`step_settle_s`（默认 **0.5**）；`plan_hold_s` 退回纯看门狗；校验改为 `action_step_s <= plan_hold_s`（原来强制两数相等）；新增 `StepController`（纯逻辑，可单测）：一个计划版本最多驱动 `action_step_s`，之后发 0，再等 `settle_s` 才算"可以看" |
| `action_adapter.py` | `discrete_to_velocity` / `trajectory_to_velocity` 的除数参数从 `plan_hold_s` 改名 `step_s`；`parse_plan` 传 `config.action_step_s` |
| `worker.py` | 新增可选 `gate`：闸关着就**不拍不送**；闸打开时先把邮箱游标推到最新（丢掉停车过程中拍的帧），等**闸开之后**的下一帧 |
| `ros_node.py` | 建 `StepController` + `threading.Event` 闸；`_on_plan` 在 armed 计划到来时关闸；`_dispatch` 用 stepper 的输出，步结束后 `ready()` 才开闸；`/ranger/vln/status` 新增 `step{lockstep,reason,action_step_s,settle_s,looking}` |
| manifest / 包内 yaml | 三个新键（一致性检查 44 → 47 键）；`action_step_s: 1.0`、`step_settle_s: 0.5`、`plan_hold_s: 1.5` 保留 |
| manifest / 包内 yaml | `guard_max_linear_accel_mps2` 0.30 → **0.60**、`guard_max_angular_accel_rps2` 0.80 → **1.20**：guard 的加速度限制是双向的，0.30 时起步斜坡要 0.83 s，会吃掉整个 1 s 步长，而且 `step_settle_s` 到了车还在滑 |
| 测试 | `StepController` 6 条（步长、慢模型只暂停不叠加、停稳才开闸、计划过期后 settle 仍有效、无计划、关掉=老行为）；配置 4 条；worker 闸 2 条（关闸不问、开闸跳过停车期间帧）；adapter/静态契约断言同步 |
| 文档 | `ARCHITECTURE.md` 第 6 节整段重写（三个数各管什么 + 走走停停流程）、动作码表与轨迹公式的除数、`DEPLOY.md` 4.4、`ACCEPTANCE.md` 待验项 |

**现在的节拍**：一步 `1.0 s` → 停 `0.5 s` → 拍图发请求（实测 ~0.78 s）→ 执行。一个周期约
**2.3 s**，车会一顿一顿地走——这是模式定义，不是卡顿。想退回"边走边问"：
`lockstep_enabled: false`。

**只有会动的计划才关闸**：STOP（全 0）和只有 5/9 的计划速度本来就是 0，车没动，下一帧的
位姿仍然是对的，所以照旧按模型节奏问（否则 `finish_after_stops` 的 20 帧 / 30 s 会被拉长到
一分多钟）。

**边界（必须知道）**：这解决的是"画面与执行不同步"，**不解决底盘闭环**。开环一步仍受加速
斜坡影响（0.25 m 的步在 1 s 内实际走 ~0.2 m），转弯同理；要真正准，下一步才是拿 `/odom`
测实际转角做补偿（方案 1）。**也没碰**避障：本栈仍是纯视觉、无雷达/Nav2。

**验证**：单测 247 跑，5 红全是 `test_set_vln_mode`（manifest 停在 `live`，切回 dry-run 即绿，
与本轮无关）；`python3 scripts/validate_manifest_consistency.py` → OK（会打印 lockstep ON 的
节拍与新的分界点）。**没有跑 live，也没有连远程服务器**。

**重启才生效**：改的是启动配置。`mode` 现在是 `live`，重启前先 `scripts/set_vln_mode.sh dry-run --yes`。

---

## 2026-10-22 · 连续 STOP 主动收尾：`finish_after_stops`（模型一直回 0 时 executor 能结束）

**用户要求**："当模型一直返回0，executor应该主动终止移动，这块逻辑需要同步到internvln_navigation"。

**问题**：DualVLN 的 0 是 **STOP——"我判定任务做完了"**，是终态判决，不是"这一帧没动作"
（5/9 才是这个意思）。以前我们只会把 0 翻成 `(0,0)` 速度：车停着，但 client 仍然 armed、
worker 仍然每帧问模型，`vln/task get` 一直回 `running=true`。executor 那边就永远等不到
"结束"，只能挂着轮询一辆不会动的车。

**改了什么**（全部在 `skill-internvln-navigation-rbnx` 内）：

| 文件 | 变化 |
|---|---|
| `core.py` | 新配置 `finish_after_stops`（默认 **20**，1..1000）与 `finish_after_stops_min_s`（默认 **30.0**，0..3600）；`InferencePlan` 加 `model_stop`；`PlanStore.update` 透传 |
| `action_adapter.py` | `parse_plan` 只在**整条列表全是 0**（含标量 0）时置 `model_stop=True`；5/9、"0 混 5/9"、1/2/3、轨迹都不算 |
| `vln_runtime.py` | `note_plan()` 计数并记录这一串 STOP 的起点；**帧数与持续时间两个条件都满足**才置 `_pending_finish`；`pump()` 消费它，做**和操作员 stop 完全一样**的两件事（退计划 + 卸 client 指令），置 `finished` 并记下结束时的帧数/时长；新指令 / `stop` / `unbind` 都会清掉完成态 |
| `ros_node.py` | 起栈时 `RUNTIME.configure(finish_after_stops=..., finish_after_stops_min_s=...)`；`_on_plan()` 里调 `note_plan`，STOP 的日志抬到 info |
| `provider.py` | `get` 新增 `status="finished"`、`running=false`、`instruction=刚结束的任务`，`detail` 写明连续 STOP 帧数与持续时长 |
| manifest / 包内 yaml | `finish_after_stops: 20` + `finish_after_stops_min_s: 30.0`（一致性检查 42 → 44 键） |
| 测试 | adapter 4 组 STOP/非 STOP；runtime 11 条（门槛、**只有帧数不够时间不结束**、清零、新指令重开、操作员 stop 优先、未绑定重排、闸门关着也不吞 finish、unbind 忘记）；provider `get` finished；core 非法值 |
| 文档 | `ARCHITECTURE.md` 动作语义 + `COMMANDS.md` / `DEPLOY.md` / `ACCEPTANCE.md` 的 `get` 状态表；顺手修正 `ARCHITECTURE.md` 里残留的"分界点 ±15.3°"，回退后是 **±20.3°** |

**判据为什么是"两个条件"**：用户明确要求"不能只要出现 0 就结束，要判定为一直无限循环为 0
的时候才终止"。第一版是连续 3 帧就收尾，太容易把正常的短暂 STOP 当成完成。现在必须
**连续 ≥ 20 帧 **且** 这一串已持续 ≥ 30 s**；中间只要来一帧 1/2/3 或轨迹，帧数和计时一起
清零。所以偶尔的 0、几帧 0 后继续走，都不会结束任务；只有真的一直卡在 0 才会收尾。

**边界**：这是**收尾语义**，不是让模型多走。模型判定到了就是到了；我们能保证的是"到了"之后
机器人**真的结束**（不发下一帧、能力报 finished），而不是假装还在跑。如果模型一上来就一直
回 0（指令没有目标、画面不对），现在会在一段持续的 0 之后明确报 `finished`——排查方向依然是
指令/画面，不是等它自己动。

**验证**：单测 235 跑，5 红全是 `test_set_vln_mode`（manifest 停在 `live`，与本轮无关）；
一致性检查 OK（44 键）。

**dry-run 实测**（容器内，临时 manifest 把门槛压到 5 帧 / 5 s 以缩短测试，模型换成本机
127.0.0.1 的桩，不连远程服务器；生产默认仍是 20 / 30 s）：

- 连续 STOP 计数涨到 11、已超过 5 帧，但只过了 3.2 s：**不结束**，`finished=false`；
- 到 5.3 s 时收尾：`finished=true`、`finished_stops=18`、`finished_after_s=5.3`，
  `armed=false`，`requests` 停在 19，之后桩没有再收到请求；
- 中途把桩切到前进动作：计数从 11 归零，任务继续；切回 STOP 后重新累计，满门槛才结束；
- 全程 `/cmd_vel` publisher 数为 0，`cmd_vel_raw` 话题不存在（dry-run 不建速度发布者）。

**生效条件**：manifest 改了，正在跑的进程不变；下次 `rbnx boot` 生效。

---

## 2026-10-22 · 转向增益整段回退：回到纯几何追踪，`wz = 方位角 / plan_hold_s`

**用户反馈**："转向幅度的问题完全回退，幅度目前太大"。

**回退了什么**：把 2026-10-22 那一串"放大方位角"的改动**全部拿掉**，不是再调一个数字：

| 位置 | 变化 |
|---|---|
| `core.py` | 删除 `trajectory_yaw_gain`、`trajectory_yaw_boost_max_deg` 两个字段和解析 |
| `action_adapter.py` | 删除 `_steer()`；`wz` 直接用方位角；note 里不再有 `yaw gain` |
| manifest / 包内 yaml | 两个键删掉（一致性检查因此从 44 键变 42 键） |
| 测试 | 删除增益/封顶/负数校验 6 条，改成"方位角原样透传"和"超过 90° 才钳制" |
| `ARCHITECTURE.md` / `COMMANDS.md` | 公式和排障表改回纯几何追踪 |

**现在**：

```
vz = 方位角 / plan_hold_s        # 3° 就是 3°，25° 就是 25°
```

原地转的分界点随之回到 **±20.3°**（`atan2(0.30, 0.81)`），之前是 15.3°。

**教训**：模型画出来的方位角就是它想转的角度。我们连着三轮（×2 → ×1.5 → ×2 封顶 +5°）都
在试图"让小角度更明显"，结果每版不是不够就是甩，最后一次的封顶版仍然嫌大。回退是最干净的
答案；以后要动灵敏度，先拿一张真实回包的轨迹点数出方位角，再决定，别凭"看起来不灵敏"调。

**验证**：单测 222 跑，6 红全部是 `test_set_vln_mode`（manifest 停在 `live` + 现场有
`ranger_base_node`，与本轮无关）；一致性检查 OK，打出
`steering by the bearing (pure pursuit); past +/-20.3 deg it pivots`。

**生效条件**：manifest 改了，正在跑的进程不变，下次 `rbnx boot` 才生效。

---


## 2026-10-22 · 关闭深度安全阀（`safety_gate_enabled: false`）——按操作员要求

**用户要求**："你把安全阀关掉"。

**改了什么**：

| 文件 | 变化 |
|---|---|
| `robonix_manifest.yaml` | `safety_gate_enabled: false`；相机 `enable_depth: false`（没人消费的流不开，也避免组件表看起来像还有反射） |
| `docs/SAFETY.md` / `README.md` / `ARCHITECTURE.md` / `ACCEPTANCE.md` / `COMMANDS.md` / `DEPLOY.md` | 明确标注"当前部署闸门已关闭"，验收项标成"闸门开启时才验" |
| `soma.yaml` | can_do/notes 标注闸门已关 |

**没改**：`core.py` / `safety_gate.py` 的默认值仍是 `enabled=True`、0.30/0.50 m；
包内配置 `internvln_navigation.yaml` 也仍是 `true`。**只有本部署的 manifest 关掉了它**，
所以重新打开只需要把 manifest 里这两个词改回来（gate + camera depth），没有别的改动。

**一致性检查为什么要求一起关深度**：脚本里有一条硬规则——相机发深度但闸门关着，说明
"有个没人读的流、组件表却像有反射"，直接 FAIL。关掉 `enable_depth` 后检查通过：
`camera wiring: front_camera camera_name -> /camera_435i/color/image_raw -> model`。

**安全后果（必须写清楚）**：关掉后机器人**没有任何近距停车反射**。模型输出到轮子之间只剩
chassis guard 的限速（0.25 m/s、0.3 rad/s）、限加速度、前向锁和 0.25 s 命令超时。
现场必须有人、手不离遥控器。

**另一个要说清的事实**：这个阀**不是**"没到花盆就说到"的原因。提前停是模型自己回的
`STOP`（视觉策略没有距离感），闸门只会在 <0.30 m 时介入，两者无关。关掉它不会让车多走
那一段，只会少一道保护。

**生效条件**：改的是 manifest，**正在跑的进程不会变**，下次 `rbnx boot` 才生效；而且
manifest 仍是 `mode: live`，重启后轮子 armed。

**验证**：一致性检查 OK（深度项按"闸门关闭"路径通过）；单测 226 跑，5 红全部是
`test_set_vln_mode`（manifest 停在 `live` + 现场有 `ranger_base_node` 在跑，与本轮无关）。

---


## 2026-10-22 · 装上语音旁路：`audio_client_bridge` + `speech`（腾讯云 ASR/TTS）

**用户要求**：在容器里把 robonix 的语音原语装上，要能用嘴交互，参考跟随栈。

**加了什么**（全在部署层，robonix 源码零改动）：

| 文件 | 变化 |
|---|---|
| `robonix_manifest.yaml` | 新增 primitive `audio_client_bridge`（reverse，`0.0.0.0:60002`）、service `speech`（tencent ASR/TTS，speaker 指向 bridge）、顶层 `env.SPEECH_BACKEND: tencent` |
| `soma.yaml` | robot 级 export `speech`（listen/speak）+ 组件 `client_audio`（mic/speaker），与 manifest 对齐 |
| `.env` / `.env.example` | 三个 `TENCENT*` 凭据（值从跟随栈 `.env` 拷来，只落在本机 `.env`）；示例文件写清用途 |
| `scripts/validate_manifest_consistency.py` | 新增 `check_voice_wiring`：半接入、transport、端口范围、speaker 指向、soma 声明；`no dead stack` 放行 `speech` |
| docs | README / ARCHITECTURE / ACCEPTANCE / COMMANDS 从"没有语音"改成"可选旁路"，并给验法 |

**为什么 `SPEECH_BACKEND` 在顶层 `env:`**：speech 的构建脚本读的是**进程环境变量**，
不是它自己的 `config:`。放在包的 config 里构建时根本看不到，腾讯依赖就不会被装。

**容器里已经 build 过的（本轮实际执行）**：

```bash
rbnx build -p rbnx-boot/cache/primitive-audio-client-bridge-rbnx --no-update-check
# ✓ Package 'robonix.primitive.audio.client_bridge' build finished
SPEECH_BACKEND=tencent rbnx build -p /root/workspace/services/speech --no-update-check
# [build] Tencent ASR/TTS imports OK; no local model or GPU required
# ✓ Package 'com.robonix.example.speech_service' build finished
```

**没做的事**：没有重启栈、没有改 `mode`。语音 provider 要下次 `rbnx boot` 才会出现；
manifest 现在仍是 `mode: live`，重启 = 轮子 armed，人不在现场应先
`scripts/set_vln_mode.sh dry-run --yes`。

**边界**：

- 语音不在控制链上；client 没连 60002 时麦克风/音箱只是哑的，底盘行为一字不变；
- ASR 只负责"听清"，把一句中文路由成导航任务靠 pilot 的 VLM——VLM 不通的现象是
  "听得到、车不动"，不是 ASR 坏了；
- 同机的跟随栈也监听 60002；两栈本来就不能同时起（都占 `can_ranger`），语音端口是第二条理由。

**验证**：单测 226 跑，6 红全部是 `test_set_vln_mode`（唯一原因：manifest 停在 `live`，与本轮
无关）；一致性检查 OK，新打出
`voice wiring: client audio on 0.0.0.0:60002 (reverse), speech -> audio_client_bridge`
和 `no dead stack: services ['speech']; 6 providers, voice is the only one off the command path`。

---


## 2026-10-22 · 转向改成"小信号增益 + 封顶"：2.9° 吃满 ×2，大弯最多 +5°

**用户反馈**："转向又不太够"。1.5 嫌不够，2.0 又嫌甩——**一个线性倍数同时满足不了两头**，
所以这次不是继续在 1.5 和 2.0 之间二分，而是换形状。

**为什么一个倍数不行**：近处方位角天生只有 2.9~7.7°，需要放大才看得见；可同一个 ×2 落到
25° 的弯上就是 50°，当然甩。真正要的是"小角度全给，大角度只给一点"。

**改法**：新增 `trajectory_yaw_boost_max_deg`（默认 **5°**），把增益变成有封顶的小信号增益：

```
wz = min(方位角 × gain, 方位角 + boost_max) / plan_hold_s      # gain 默认 2.0
```

| 方位角 | 纯几何 | ×2（上一版） | **现在** |
|---|---|---|---|
| 2.9°（0.3 m 实测） | 2.9° | 5.8° | **5.8°** |
| 7.7°（1.0 m 实测） | 7.7° | 15.4° | **12.7°** |
| 12° | 12° | 24°（要原地转） | **17°（还能走弧）** |
| 25° | 25° | 50° | **30°** |
| 原地转分界点 | ±20.3° | ±10.2° | **±15.3°** |

封顶设成 90° 就退回纯倍数；gain 设 1.0 就退回纯几何追踪。两个都是 manifest 里的参数。

**同步改的**：`core.py`（新字段 + 解析，允许 0~90）、`action_adapter.py`（`_steer()` 纯函数，
`parse_plan` 传参）、三层配置、`validate_manifest_consistency.py`（分界点按
`max(limit/gain, limit-boost)` 算，打出 `at most +5 deg`）、`ARCHITECTURE.md` / `COMMANDS.md`。

**测试**：`test_action_adapter` +2（12° 被封顶成 17° 且仍走弧；负封顶报错）、clamp 测试改成
显式放开封顶；`test_core` +2 条非法值。226 跑，6 条红仍是 `test_set_vln_mode`（manifest 停在
`live`）。一致性检查打出 `steering by the bearing x2 (at most +5 deg); past +/-15.3 deg it
pivots`，44 键 OK。

**上线**：需要重启 VLN primitive。

---

## 2026-10-22 · 安全阀停车距离 0.50 m → 0.30 m

**用户要求**：把 0.5 米的闸门距离改成 0.3。

**改法**：`safety_gate_stop_m` 0.50 → **0.30**，回差保持 **20 cm**（这是闸门自己的设计约束：
放行距离必须大于停车距离，否则在阈值上反复开关），所以 `safety_gate_release_m` 跟着
0.70 → **0.50**。三层配置一起改（`core.py` / `robonix_manifest.yaml` / 包内 yaml），
`safety_gate.py` 里的 `SafetyGateConfig` 默认值同步。

| | 原来 | 现在 |
|---|---|---|
| 停车 | < 0.50 m | **< 0.30 m** |
| 放行 | ≥ 0.70 m | **≥ 0.50 m** |
| 回差 | 20 cm | 20 cm |

**要知道的代价**：闸门更靠近车身了。0.2 m/s 下，命令超时 0.25 s 之内还能滑 5 cm，
0.30 m 的余量仍然够停下，但**留给"发现—反应"的距离少了 20 cm**。这本来就是一道
最后一米的反射，不是避障；它在 0.3 m 才拦，等于承认 0.3 m 以外的一切全靠模型和现场盯人。

**测试**：`test_safety_gate` 的样本距离整体下移（近距 0.5 m → 0.2 m，回差段 0.6~0.8 →
0.4~0.6），`test_vln_runtime` / `test_provider_contract` 里的 `nearest=0.42` → `0.22`。
20 + 17 + 15 条全绿。一致性检查打出 `stop < 0.3 m, release > 0.5 m`，43 键 OK。

**同步改的**：`SAFETY.md`（§1.1 表和 §6 故障表）、`ACCEPTANCE.md`（dry-run 手测和 live 验收）、
`README.md`。

**上线**：需要重启 VLN primitive。

---

## 2026-10-22 · `trajectory_yaw_gain` 2.0 → 1.5（2.0 太冲）

**用户反馈**："幅度又太大了"。上一版把增益直接翻倍（2.0），每个缓弯都被放大成甩头。

**改法**：只改默认值，三层配置一起改（`core.py` / `robonix_manifest.yaml` / 包内 yaml），
公式和边界逻辑一个字没动：

```
wz = 方位角 × 1.5 / plan_hold_s
```

| 方位角 | gain 1.0（原来） | **gain 1.5（现在）** | gain 2.0（上一版） |
|---|---|---|---|
| 2.9°（0.3 m 处实测） | 0.034 | **0.051** | 0.067 |
| 7.7°（1.0 m 处实测） | 0.090 | **0.134** | 0.179 |
| 原地转分界点 | ±20.3° | **±13.5°** | ±10.2° |

原来的问题是"看不出来在转"（0.034 → 视觉上等于直走），2.0 的问题是"转过了"，
1.5 取中间：同样的方位角比几何追踪多转一半，但不像 2.0 那样每个弯都要甩一下。

**同步改的**：`test_action_adapter` 里那个端到端断言（配置值确实传到适配器）跟着从
`2.0 ×` 改成 `1.5 ×`；`ARCHITECTURE.md` / `COMMANDS.md` 里的默认值和分界点。
测试：`test_action_adapter` 45 条全绿，全套仍是 6 条红（全在 `test_set_vln_mode`，
因为 manifest 停在 `live`，与本次改动无关）。一致性检查 43 键 OK。

**没改的**：公式、±90° 截断、`max_wz` 钳位、原地转判定、离散动作那条路——都还是原样。

**上线**：需要重启 VLN primitive。仍嫌急就继续往下调（1.2），嫌不灵敏就回到 1.5 以上。

---

## 2026-10-22 · 轨迹转向加灵敏度：`trajectory_yaw_gain`（默认 2.0）

**用户反馈**：`y` 换算成 `wz` 之后左右转依然不灵敏。

**为什么**：模型轨迹近处的方位角天生就很小——现场实测 0.3 m 处 **2.9°**、1.0 m 处 7.7°。
按增益 1 换算，`wz = 2.9° / 1.5 s ≈ 0.034 rad/s`，1.5 s 只转 2.9°，车看起来和直走没区别。
方向本身没错（`y<0` 右转，见下一条），错的是**幅度**。

**改法**：新增配置 `trajectory_yaw_gain`，作用在方位角上，再除以 `plan_hold_s`：

```
wz = 方位角 × trajectory_yaw_gain / plan_hold_s
```

- 默认 **2.0**（翻倍）；`1.0` = 改动前的纯几何追踪；
- 仍受 `max_wz` 和底盘最小转弯半径约束：增大的 `wz` 会让弧更紧，半径跌破 0.81 m 时
  照旧**明着**改成原地转；
- 增益后的方位角截在 **±90°**，再大就只是原地打转，没有意义；
- 只影响 `trajectory` 回包；离散动作码 1/2/3 的 15° 步长不变。

**效果**（gain=2.0，lookahead 0.3 m，vx=0.2 m/s）：

| 方位角 | 原来 wz | 现在 wz | 原来半径 | 现在半径 |
|---|---|---|---|---|
| 2.9° | 0.034 | 0.067 | 5.95 m | 2.97 m |
| 7.7° | 0.090 | 0.179 | 2.23 m | 1.12 m |
| ≥10.2° | 走弧 | **原地转** | — | — |

觉得转得太急，把 manifest 里的 `trajectory_yaw_gain` 调到 1.5 即可，不用改代码。

**同步改的**：三层配置（manifest / 包内 yaml / `RuntimeConfig` 默认值）、
`action_adapter.trajectory_to_velocity(yaw_gain=…)`、`parse_plan`、validator 的分界点输出
（现在按 gain 折算，打出 `past +/-10.2 deg`）、`ARCHITECTURE.md` / `COMMANDS.md`。

**测试**：`test_action_adapter` +4（gain 翻倍且符号不变、右手路径同样加倍、增益后截到 90°、
非正 gain 报错）再 +1 端到端（配置值确实传到适配器）；`test_core` +2 条非法值。224 跑，
6 条红全是 `test_set_vln_mode`（manifest 还是 `live`）。

**上线**：配置 + 进程内逻辑，**重启 VLN primitive** 生效。

---

## 2026-10-22 · 闸门停车距离 0.80 m → 0.50 m；确认轨迹 `y<0` 是右转

**用户要求**：安全阀阈值太远，改成 0.5；另外确认模型返回的途经点 `[x, y]` 里 `y` 为负号时
是左转还是右转。

**阈值改动**（三层配置一起改，不留"改一层"的隐患）：

| 层 | 文件 | 值 |
|---|---|---|
| manifest | `robonix_manifest.yaml` | `safety_gate_stop_m: 0.50`、`safety_gate_release_m: 0.70` |
| 包内 yaml | `config/internvln_navigation.yaml` | 同上 |
| 代码默认 | `core.py` / `safety_gate.py` | 同上 |

回差保持 20 cm（0.50 停、0.70 放），不是为了省事：没有回差的话车会在 0.49/0.50 之间反复
开关、一顿一顿地蹭。`test_safety_gate` 里所有依赖距离的用例同步改成 0.4/0.6/0.8 m 三个点
（挡住 / 回差区间 / 放行），20 条全绿。

**顺带的现场效果**：之前日志里那个 0.98 m 的地板读数，新阈值下（0.98 > 0.50）不再算挡住。
代价是 0.50~0.98 m 之间不再保护——第一次 live 时人离车更近才会触发，遥控器仍然不能离手。

**转向确认：`y < 0` 是右转，`angular.z < 0`。** 依据是上游两处独立实现：

1. `InternNav/internnav/model/utils/vln_utils.py:106-113`：`target_yaw = atan2(dy, dx)`，
   `delta_yaw > 0 → 动作 2（左转）`，`delta_yaw < 0 → 动作 3（右转）`；
2. 官方 Go2 客户端 `InternNav/scripts/realworld/http_internvla_client.py:158-172`：
   把每个 `[x, y]` 用标准 `w_T_b`（REP-103，yaw 逆时针为正）变换到世界系，
   车体系 `+y` 就是车的左侧，`y<0` 在右侧。

我们 `action_adapter.py` 用的也是 `atan2(y, x)`（`trajectory_to_velocity`），方向与上游一致；
`tests/test_action_adapter.py` 里有一条镜像用例（`leftward` 与 `rightward`）盯着这件事，
防止哪天有人把 `|y|` 带进来。

**上线**：同样是进程内 + 配置改动，**必须重启 VLN primitive**。manifest 现在还是 `live`。

---

## 2026-10-21 · 闸门关闭时停掉原语并向 Robonix 报 `blocked`，不再空转请求 VLN

**用户要求**：安全阀关闭时，Robonix 要主动停止原语的执行、反馈当前状态，而不是一直循环地
请求 VLN 执行下去。

**之前的问题**：闸门只做了一件事——把 `_dispatch` 发出去的速度换成零。模型那边完全不知情：

- `vln/task get` 还报 `running=true`（`armed` 仍是 true）；
- 推理 worker 还在每帧 POST `/eval_dual`，远端一直在被问；
- 计划还在，人一走开闸门一开，旧计划又接着跑——安全停一次等于没停过。

**改法**（全部在我方 provider 包内，Robonix 框架源码一行没动）：

| 文件 | 改动 |
|---|---|
| `vln_runtime.py` | 新增 `set_safety_state()` / `reset_safety()`：维护闸门镜像状态，**只在判定翻转的那一 tick** 动手 |
| `ros_node.py` | `_dispatch` 每 tick 把闸门判定同步给 `RUNTIME`；`_log_safety` 改为接收 `changed` |
| `provider.py` | `get` 在 `safety.blocked` 时报 `status="blocked"`、`running=false`；`set` 在挡住时返回 `status="held"` |
| `task.v1.toml` / `VlnTask.srv` | 只改注释与 description，**字段和 IDL 没变** |
| `soma.yaml` | 本体描述同步：补 `camera/depth` 能力、改掉"没有近距传感器"的旧说法、加 can_do/notes |

**闸门关闭那一 tick 具体做什么**：

1. `plan_store.clear()` —— 正在执行的计划作废，速度归零；
2. `client.clear_instruction("safety gate closed: <reason>")` —— **解除武装**。
   这一步才是"停止循环请求 VLN"的关键：worker 没收武装就不再发 `/eval_dual`；
3. `status` 计数 +1，记下 `reason` / `nearest_m` / 开始时间。

**放行时**：只翻转状态，不自动武装；被撤销的那个任务**不会自己恢复**（回到
`awaiting-instruction`，要动得重发）。挡住期间 `set` 的指令会记成 `held_instruction`，
放行后由下一个 `pump()` 自动应用——它从没启动过，不像"恢复"。

**对外表现**：

- `robonix/primitive/vln/task get` → `status="blocked"`、`running=false`，
  `detail` 带 `reason=… nearest=… stop=… blocked_for=…s stops=…; no model requests are being made; …`；
- `set` → `status="held"`（接受，但没开始）；
- `/ranger/vln/status` 的 `operator.safety` → `blocked/reason/nearest_m/blocked_for_s/stops/held_instruction`；
- `scripts/watch_vln_chain.py` 多打一行 `robonix task: HELD -- execution halted …`。

dry-run 同样生效（dry-run 也会请求模型，这条规则主要就是冲它去的）。

**测试**：`test_vln_runtime` +7（停任务/只停一次/挡住时 held/queued 被 held/pump 竞态保护/
操作员 stop 丢弃 held/unbind 清状态）；`test_provider_contract` +2（`get` 报 blocked、
挡住时 `set` 报 held 且放行后生效）；`test_static_contract` +1 并改 1（dispatch 里必须调
`RUNTIME.set_safety_state`、仍不许直接碰 `_plans`）。全仓 219 跑；除 5 条
`test_set_vln_mode`（唯一原因是 manifest 现在是 `live`）外全绿。

**上线**：改的是进程内逻辑，**必须重启 VLN primitive 才生效**。manifest 现在是 `mode: live`，
live 下重启会重新武装底盘——重启前确认前方净空、遥控器在手，或者先切回 dry-run 验一遍
`get` 的 `blocked`/`held`。

---

## 2026-10-21 · 修闸门"启动闭锁粘连"：深度 0.98 m 也一直 BLOCKED

**现场现象**：切到 live 后模型一直给前进（`vx=0.200`），车不动；日志每 20 ms 一条
`proximity gate BLOCKED: reason=obstacle, nearest=0.98m`。0.98 m **大于** 0.80 m 的停车线，
看起来像深度在乱报。

**根因**：不是深度读数错，是我把两种"挡住"混成了一个状态位：

1. 开机时还没有深度帧 → 闭锁逻辑把 `_blocked` 置成 `True`（`reason=no_depth`，正确）；
2. 第一帧有效深度 0.98 m 到达 → 回差逻辑看到 `_blocked=True`，要求退到 **1.00 m** 才放行；
3. 画面里那个 0.98 m 是静止的（很可能是 ROI 下边缘的地板），永远退不到 1.00 m →
   **一直锁死**。模型给什么都被覆盖成零速度。

**修法**：把"障碍闩锁"和"缺数据闭锁"分开：

- `_obstacle_latched` 只在 `nearest <= stop_m` 时置位，**只有它**参与 0.80/1.00 的回差；
- 缺帧、过期、坏帧仍然 fail-closed（`_blocked = require_depth`），但**不置障碍闩锁**；
- 因此缺数据恢复后的第一帧按自己的距离判定：0.98 m > 0.80 m → `clear`；
- 真障碍的闩锁**跨缺数据保留**：0.5 m 挡住 → 断流 0.5 s → 回来 0.9 m 仍然挡住 → 1.1 m 才放。
  这是对的：断流不代表障碍消失。
- `reset()` 同时清掉闩锁。

**测试**：`test_safety_gate` 加两条——
`test_a_valid_frame_after_a_fail_closed_start_is_judged_on_its_own`（0.98 m 必须放行）、
`test_missing_depth_does_not_release_an_obstacle_latch`（真障碍的闩锁跨断流保留）。共 20 条全绿。

**上线**：改的是节点进程里的逻辑，**必须重启 VLN primitive / 整个栈才生效**。重启后预期日志：
先 `no_depth` 闭锁约 2 s，然后 `proximity gate clear: reason=clear, nearest≈0.98m`，接着
`/cmd_vel` 才有 `vx`。live 下重启会重新 armed，重启时确保前方净空、遥控器在手。

**待确认（不影响本次修复）**：0.98 m 这个固定值如果是 ROI 下边缘扫到的地板（相机高约
0.37 m 时几何上正好对得上），说明 `roi_bottom_fraction: 0.80` 离停车线太近（地板 0.98 m，
停车 0.80 m）。若现场确认前方空无一物仍长期读到 ~0.98 m，把 `roi_bottom_fraction` 降到
0.75（地板约 1.18 m）或 0.72 会更稳；代价是 0.8 m 处低于 ~11~15 cm 的低矮物看不全。

---

## 2026-10-21 · 加深度近距反射闸门：模型说什么，都不能撞上去

**用户要求**：车偶尔遇到障碍物不停，先加一道"安全阀"，不能出现撞墙。

### 为什么

DualVLN 是个**纯视觉策略**：只吃一帧 RGB、没有深度、没有雷达，回答频率还是 0.5~3 s 一次。
它输出的"该往哪走"里**不包含"这里安不安全"**。快系统那边更直接——32 条候选轨迹是
`np.mean` 平均成一条的（`vln_utils.traj_to_actions`），**没有任何安全评分挑选**，障碍物在
平均里只会被抹淡。所以"偶尔不停"不是 bug，是这套结构的天花板。

### 做了什么

新增 `packages/internvln_navigation/internvln_navigation/safety_gate.py`（纯 numpy，不 import
rclpy），在 50 Hz 派发线程里、**发布之前**评估，挡住就发零速度：

- **看哪里**：深度图中间 60% 宽、25%~80% 高的前向带。下边界停在 0.80 是因为镜头正下方的
  地板永远在一米内，圈进来就永远停。
- **怎么判**：带内取第 `safety_gate_percentile`（默认 1）百分位，单点飞像素不会误触发，
  但细腿/桌角能看见。
- **阈值 + 回差**：< `stop_m`（0.80 m）停，≥ `release_m`（1.00 m）才放；中间保持原状态，
  否则车会在阈值上反复开关、一顿一顿地蹭。
- **只换命令，不动计划**：`PlanStore` 照常跑、episode 照常续，障碍物走开后自动恢复，
  不会因为一次靠近就把任务作废。
- **闭锁（fail-closed）**：没有深度帧 / 帧超过 `depth_timeout_s`（0.30 s）/ 可用像素不足 /
  编码不认识——**全部按"挡住"处理**。车不动是部署问题，撞墙是安全事故。
- **深度不进模型**：`enable_depth: true` 只是让相机多推一路话题，模型输入仍是那一帧 RGB。

### 改了哪些文件

| 文件 | 变化 |
|---|---|
| `internvln_navigation/safety_gate.py` | 新增：`SafetyGateConfig` / `SafetyGateDecision` / `DepthSafetyGate` |
| `internvln_navigation/core.py` | `RuntimeConfig` 加 12 个 `safety_gate_*` / `depth_topic` 字段 + 交叉校验（release > stop、ROI 上 < 下） |
| `internvln_navigation/ros_node.py` | 深度订阅（在 live 闸门外，dry-run 也能观察）、`_dispatch` 里先 `check()` 再发、`status.safety` |
| `config/internvln_navigation.yaml` | 12 个键与字段**逐一对齐**（静态测试要求 exact parity） |
| `robonix_manifest.yaml` | `enable_depth: true`、`depth_topic`、`safety_gate_*`；`mode` **未动** |
| `scripts/validate_manifest_consistency.py` | 开闸就要求 `enable_depth: true` 且 `depth_topic` 与 `camera_name` 对得上；关闸却开深度也报错 |
| `tests/test_safety_gate.py` | 新增 18 条：停车/放行/回差/毫米与浮点/ROI 排除/无深度/过期/坏帧/配置校验 |
| `tests/test_core.py` | 配置默认值与交叉校验用例 |
| `tests/test_static_contract.py` | 深度订阅必须在 live 闸门外；`_dispatch` 必须用闸门结果覆盖命令、不得顺手清计划；`status` 必须带 `safety`；`safety_gate.py` 进"离线模块不许 import rclpy"清单 |

### 它不是什么（别当成避障用）

- **只停不绕**：障碍物不动，车就一直停着等人；
- **只看前向带**：侧擦、后向它管不了；
- **低矮 / 透明 / 反光**（玻璃门、镜面、深色地毯边）深度可能没有回波——**第一次 live 仍然
  要人在场、遥控器在手**（`docs/SAFETY.md` §1.1 已写）。

### 验证

- `python3 -m unittest discover -s tests -q`：**207 跑**，5 红，全部是
  `test_set_vln_mode`，原因是 manifest 停在用户手动设的 `mode: live`（与本改动无关，
  切回 dry-run 会自己转绿）。新加的 `test_safety_gate` 18 条全绿。
- `python3 scripts/validate_manifest_consistency.py` → **OK**，新增一行
  `front_camera depth -> /camera_435i/depth/image_raw -> proximity gate`。
- 没有连远端服务器、没有启动任何栈、**没有让机器人动过**。

### 上线前提（操作层面）

1. `enable_depth` 是**相机 primitive 的启动参数**，必须**重启相机 provider / 整个栈**才生效；
2. 深度没通时 live 下闸门是**闭锁**的——车一步都不会动。先 dry-run 看
   `status.safety.reason` 是 `clear` 而不是 `no_depth`；
3. manifest 当前是 `mode: live`（用户此前手动切的）。**下次重启 = 底盘 armed、轮子能转**。
   按既定安全约束，重启前先 `scripts/set_vln_mode.sh dry-run --yes`。本轮没有改这个值。

---

## 2026-10-16 · 轨迹换成"走弧线"：删掉 15° 转弯阈值（车一直直走的真凶）

现场实测：模型的 `trajectory` 明明是一条**往左弯的弧**，车却一直直着往前走。查下来不是模型
不拐，是**我们把它的转弯信息扔了**。

- **病根**：老换算里有个 15° 门槛——方位角不到 15° 就 `wz = 0` 纯直走。而模型这条轨迹
  **近处天生就几乎不偏**：它描述的是"车接下来会怎么动"，车不可能瞬间改朝向，所以线是从当前
  朝向平滑铺出去的。实测数据（用户现场抓的两条回包）：
  - 往左那条：0.3 m 处偏 **2.9°**、1.0 m 处 7.7°、1.5 m 处才到 12.5°；
  - 笔直那条：0.3 m 处偏 **-0.9°**。
  我们只看最近那个 0.3 m 的点 → 每一帧都读到"两三度" → 永远不到 15° → **永远直走**。
  它还是个自我稳定的循环：这一帧没转，下一帧模型给的还是从当前朝向铺出去的线，近处照样不偏。
- **参照官方实现**：`internVLN/InternNav/scripts/realworld/http_internvla_client.py:157`
  拿到轨迹后 `if i < 3: continue`——**前 3 个点直接扔**（嫌近处没信息量），剩下的点用里程计
  转到世界坐标，整条交给 MPC 当参考轨迹跟。人家是"照这条线走"。顺带确认了方向约定：
  它乘了机器人朝向的旋转矩阵，说明这套点就是本体坐标系 +x 前 +y 左，和我们一致。
- **新算法（一句话）**：取第一个 ≥ `lookahead_m`(0.30 m) 的点，在**一个 `plan_hold_s` 内既转过去
  又走过去**——`vx = 前进量/hold`、`wz = 方位角/hold`。这样算出来的半径 = `前进量/方位角`，
  正好是穿过那个点的那条弧，也就是**跟着模型画的线走**。
- **没有阈值了，故意的**：3° 就是 3° 的偏转（实测那条给出 `wz=+0.0336`、半径 5.95 m 的宽弧），
  真的笔直才算 0（那确实是 0）。这是本次改动的核心。
- **太大转不过来的才原地转**，判据来自底盘而不是拍脑袋：Ranger 在
  `|linear.x|/|angular.z| < min_turn_radius` 时会**默默丢掉 `linear.x`**，弧线命令会变成没人要的
  原地转。所以先把命令按 `max_vx/max_wz` 钳好，再问"这条弧半径 ≥ 0.81 m 吗"：够就照走，
  不够就**明着**下原地转命令（note 写 `pivoting`），下一帧从新朝向重推。本仓这组数字下分界点在
  **±21.2°**（`lookahead / guard_min_turn_radius_m`）。注意判据用的是**钳过之后**的命令，
  免得"钳 vx 反而把弧压得比底盘还紧"。
- **删掉 `turn_threshold_deg` 整个配置项**（manifest、包内 yaml、`RuntimeConfig` 三层都删）：
  它现在只剩误导。manifest 校验脚本的 `check_config_keys` 会把 manifest 里对不上字段的 key
  报出来，所以三层必须同步删。原判据改由 `guard_min_turn_radius_m`（一个来源）现算。
- **校验脚本加了一行派生量**：`trajectory arc -> 0.2000 m/s over a 0.3 m lookahead … past
  +/-21.2 deg it pivots in place instead`，外加"弧速超过 `max_vx` 就退回原地转"的 NOTE。
- **`lookahead_m` 的定位也写清了**：命令半径 = `前进量/方位角`，对同一条曲线**与 lookahead 无关**
  （实测：0.3/0.6/1.0 m 三个 lookahead 给出的都是 5.95 / 8.13 / 7.39 m 这个量级），所以它是
  **平滑旋钮**（看远点=平均更多路径、更抗噪），不是转向增益。**不用**靠调大它来"让它拐"。
- **测试**：`test_action_adapter.py` 的轨迹用例重写——旧的两条（"大角度→原地转"、"小角度→不转"）
  premise 本身就废了。新增/改成：3° 必须给出正 `wz`、**零点附近没有死区**（0.2/1/3/10° 都有比例
  yaw）、右偏给出负 `wz`、命令半径 = `前进量/方位角`、20° 走弧 / 30° 原地转（分界点两侧）、
  `max_vx` 钳到 0.05 后连 10° 都转不动只能原地转。`test_core.py` 里那条拿 `turn_threshold_deg`
  当非法值的用例换成 `lookahead_m` / `guard_min_turn_radius_m`。
- **验证**：182 跑，只剩那 5 个 `test_set_vln_mode` 红（manifest 停在 `mode: live`，与本改动无关）；
  `validate_manifest_consistency.py` → OK。把用户现场抓的两条真回包丢进 `parse_plan` 复算：
  往左那条 → `vx=+0.2000 wz=+0.0336`（5.95 m 左弧，过去是 0）；笔直那条 → `vx=+0.2000
  wz=-0.0104`（19.16 m 极缓右弧）。**两条都不再是"直走"**。
- **文档同步**：`ARCHITECTURE.md`（§2 新增"轨迹那条路：走模型画的弧"，并删掉"转向一定是纯原地
  旋转"这个已不成立的全称说法）、`COMMANDS.md`/`ACCEPTANCE.md`（"转一条"的判据改成"两种回包都算
  通过"）、`RUNBOOK.md`/`DEPLOY.md`/`COMMANDS.md` 的排障行（`vx=0 wz≠0` 现在有三个正常出处）、
  `soma.yaml`（声明走弧行为）。

### 正负号复核（用户追问"二元数组的正负有没有考虑过／为什么只会左转"）

结论：**代码全程对称，没有取绝对值、没有只认正的、没有隐性偏左**。逐层查证并加了回归测试：

- **唯一权威判据**：官方自己的换算函数 `vln_utils.py:63` `traj_to_actions` →
  `trajectory_to_discrete_actions_close_to_goal`：`target_yaw = np.arctan2(dy, dx)`，
  `delta_yaw > 0` → 动作码 `2`，`< 0` → `3`；而 `actions2idx`（
  `internvla_n1_agent_realworld.py:61`）写着 `"←": [2]`、`"→": [3]`。**+y = 左**，
  与 ROS REP-103 一致，也正是本模块 `wz = atan2(y, x)/hold` 的符号。
- 官方 Go2 客户端 `http_internvla_client.py:157` 把 `(traj[0], traj[1])` 直接乘 `w_T_b`
  （标准 `[[cos,-sin],[sin,cos]]`），也没取反——**再次确认点就是本体系 +x 前 +y 左**。
- 链路每一段的符号都保住了：`_finite_float` 只拒绝非有限值（负数照收）、`VelocityCommand.clipped`
  是 `max(-m, min(m, v))` 的对称钳制、原地转分支直接用 `math.radians(bearing)`（没 `abs`）、
  底盘 guard 的 `clamp_yaw` 用 `math.copysign(..., wz)`。
- **实测复算**（把用户抓的两条真回包喂给 `parse_plan`）：往左那条 `wz=+0.0336`；把它的 y 全部
  取反后得到 `wz=-0.0336`，**恰好等值反号**、`vx` 完全相同。而第 1 条（y 前段为负）在
  `lookahead=0.30/0.50/0.60` 下给的是 **`wz=-0.0104 / -0.0115 / -0.0043`，即右转**——
  **右转这个分支是真的会出来的**，只要有数据让它出来。
- **那为什么现场看着像"只会左转"**：因为模型这两条回包本身就是偏左的。第 2 条 33 个点里
  `y > 0` 的有 32 个、`y < 0` 的 0 个；第 1 条 27 正 5 负，且负值只有 -0.009 m 量级。
  模型没说往右，车当然不往右——**这不是换算偏左，是数据偏左**。
- 新增两条回归测试：`test_the_sign_of_y_is_the_sign_of_the_turn_end_to_end`（用真回包 + 镜像，
  断言 `wz` 等值反号、`vx` 相同）、`test_a_point_behind_the_robot_is_turned_to_not_driven_through`
  （点在身后 → 只能原地转，`vx == 0`，**且转向符号仍是点的符号**）。

---

## 2026-10-16 · 发现（无本仓代码改动）：服务端把 instruction 写死了，我们发的指令它根本没读

查正负号时顺带在本地权威副本里看到的：`InternNav/scripts/realworld/http_internvla_server.py`

- **第 40 行** `instruction = "Turn around and walk out of this office. Turn towards your slight
  right at the chair. ... Stop at the computer monitor"` —— 这是**字面量**；
- **第 41 行** 只从请求里取了 `data['reset']`，全文件再无 `data['instruction']`；
- 而部署文档 `InternVLA-N1-DualVLN-部署文档.md:222` 写的是"`instruction`（导航指令；未传时用
  脚本内置默认指令）"——**文档说的和这份代码不一致，跑起来的是代码**。
- 影响：如果算力服务器上跑的就是这份文件，那么我们（以及 `vln_query.py`、官方 Go2 客户端）
  发过去的 instruction 一律被丢弃，模型永远在走它自己那句办公场景路线。**这解释了为什么
  换指令、改默认指令在行为上完全看不出区别**（上一轮把默认指令改成 `Go to the chair and stop.`
  的那些改动，在这份服务端上等于没改）。
- **需要在服务器上花 10 秒确认**（我不能连远程）：在模型仓里跑
  `grep -n 'data\[.instruction.\]\|instruction =' InternNav/scripts/realworld/http_internvla_server.py`。
  若确为该版本，正确的做法是在服务端把第 40 行改成 `instruction = data.get('instruction') or <默认句>`，
  客户端一行都不用动。

---

## 2026-10-16 · 没人下单就一个请求都不发：加 `require_instruction` 闸（开机待命）

用户要求：**指令只能从 Robonix client 或 `rbnx chat` 发出来才开始调 VLN 服务，否则不请求模型、
原地等。** 这条同时把上面两条留的尾巴收了（"无限 0"里那条"车停着画面不变、答案永远一样"的
死循环，以及 `stop` 停下之后下一帧又被同一个答案开起来）。

- **新增配置 `require_instruction`（默认 `true`）**，三层同一根链：
  `robonix_manifest.yaml`、`packages/internvln_navigation/config/internvln_navigation.yaml`、
  `RuntimeConfig`（`core.py`，带 `_explicit_bool` 校验，写非 bool 直接抛）。
  置 `false` 就退回老行为（一上电就对着 manifest 里的 `instruction:` 跑），台架调试用。
- **客户端多了一道"武装"闸**（`http_client.py`）：`_armed` + `_pending_instruction`。
  未武装时 `evaluate()` **直接返回零速度计划，连 socket 都不开**——不是"发了请求拿到 0"，
  是**一个字节都不发**（新测试 `test_an_unasked_stack_sends_nothing_at_all` 就是数请求次数的）。
  `set_instruction` 武装；新增 `clear_instruction()` 收枪。新增 `armed` 属性与
  `snapshot()` 的 `armed` / `require_instruction` 两个字段。
- **manifest 里的 `instruction:` 降级为参考文本**：它不再能自己触发任何请求，只出现在 status 里，
  方便你看"当前名义上要去哪"。开机若未武装，`ros_node` 会打一行 INFO 说明模型不会被问、
  并给出三条下单方式。
- **`stop` 现在把任务也收走**（`vln_runtime.pump()` 的 stopping 分支调
  `client.clear_instruction(...)`）。这是修一个**真实的潜在事故**：老 `stop` 只撤计划，
  下一帧照样拿同一个问题去问模型、照样拿到同一个答案、于是车又开起来——停住的寿命只有一个
  推理周期。现在撤计划 + 收枪 + 清计划表，之后就等新指令。
  （`InstructionSink` Protocol 增加 `clear_instruction`，两个 Fake 同步。）
- **`vln/task` 的 `get` 说实话**：未武装时 `status="awaiting-instruction"`、
  `running=False`、`detail` 里带 `armed=False`。不再把一个空转的 `idle` 当成"在跑"。
- **测试**：`tests/test_http_client.py` 新增 `AwaitingInstructionTests`（8 条：一个字节都不发、
  配置里的指令不会自己武装、指令在下一帧开 episode、清指令后停止请求、清指令时在飞的计划被丢、
  闸可以关掉、snapshot 两侧都报、`require_instruction` 必须 bool）；
  `test_provider_contract` / `test_vln_runtime` 的断言同步到新契约。
  旧测试的 `_client` helper 统一 `setdefault("require_instruction", False)`，否则它们全都变成"不发请求"。
- **文档同步**：`README.md`（新增"Nothing is asked until somebody asks"）、`CAPABILITY.md`
  （新增 "Idle until asked"）、`ARCHITECTURE.md` §5 新增"没人下单，就一个请求都不发"、
  `DEPLOY.md`（§8.2 开机状态、§8.3 链路重写、软停那行）、`COMMANDS.md` §7.3 重写、
  `RUNBOOK.md` §2 探测样例与 §5 发指令一节。
- **验证**：176 跑，仅剩 5 个 `test_set_vln_mode` 红（**唯一原因是 manifest 现在是 `mode: live`**，
  那几条假定起点是 `dry-run`，与本改动无关）；`python3 scripts/validate_manifest_consistency.py` → OK。
- **注意**：闸是**启动时**读的配置，生效要**重启栈**；manifest 现在是 `mode: live`，
  重启前先 `scripts/set_vln_mode.sh dry-run --yes`（人不在现场不许 armed）。

---

## 2026-10-16 · 默认指令换成目标句：`Walk forward along the path` → `Go to the chair and stop.`

接着上一条"无限 0"往下查，翻源文档发现**病根是默认指令本身**，不是链路也不是请求参数：

- **DualVLN 是到达型模型**：它拿指令当"目标",判断"到了没"，到了就吐 `STOP`。
  `Walk forward along the path` 既没有目标物、也没有终点——车本来就在路上、本来就朝前，
  模型几乎在**任何一帧都能判它完成**。车一动、画面一变 → 立刻 `STOP` → 锁死循环。
- **官方/源文档用的都是目标句**，这是这次改动的依据：
  - 部署文档 §9 的验机例子：`"Go to the door and stop."`
    （`internVLN/InternVLA-N1-DualVLN-部署文档.md:294`）；
  - 服务端内置默认指令（不传 instruction 时生效）：一整段办公场景路线
    `"Turn around and walk out of this office. … Stop at the computer monitor"`
    （`internVLN/InternNav/scripts/realworld/http_internvla_server.py:40`）；
  - 官方 Go2 客户端 `http_internvla_client.py` 里**一个 instruction 字段都没有**（grep 无命中）,
    它吃的就是服务端写死的那句长指令；
  - 厂商真实流程是"下任务"而非"喊口号"：`运行指令参考.md` 里
    `--task-source terminal` = 终端手打指令，`http` = 往 `/task/start` 发任务。
- **一句话澄清**：`Walk forward along the path` 在 `internVLN/` 那棵树里**一次都没出现**——
  它是我们自己写进 ranger 部署的，**不是官方示例指令**。
- **改了哪三层**（同一条链，必须一起改，否则留一个"改了一层、另一层还写着旧句子"的坑）：
  `robonix_manifest.yaml`（启动时真正生效的那层，并加了"为什么必须是目标句"的注释）、
  `packages/internvln_navigation/config/internvln_navigation.yaml`（包内默认值）、
  `packages/internvln_navigation/internvln_navigation/core.py` 的
  `RuntimeConfig.instruction` 代码兜底值。
- **同步的引用**：`tests/test_http_client.py` 里 5 处拿默认值当基准的断言、
  `scripts/probe_internvla_server.py` 的 `--instruction` 默认值与 docstring 例子、
  `.env.example` 示例值、`docs/DEPLOY.md` 手搓 curl 的例子、`docs/COMMANDS.md` 两处、
  `scripts/set_vln_instruction.sh` 的 usage 例子。
  历史记录性质的旧句子（`docs/RUNBOOK.md`、`docs/DEPLOY.md` 的状态快照、本文件的日志引用）
  **故意没动**——那是当时真实发生过的输出。
- **验证**：`python3 -m unittest discover -s tests -q` → `test_http_client`(19)、
  `test_vln_runtime`(10) 全绿；`python3 scripts/validate_manifest_consistency.py` → OK。
  （另有 5 个 `test_set_vln_mode` 红点，唯一原因是 manifest 现在停在 `mode: live`，
  而它们假定起点是 `dry-run`——与本次改动无关，切回 dry-run 即绿。）
- **生效前提**：改的是**启动时默认值**，要**重启栈**才生效；而 manifest 现在是 `mode: live`，
  live 下重启 = 底盘 armed。按"人不在现场必须 dry-run"的约定，**重启前先
  `scripts/set_vln_mode.sh dry-run --yes`**。不想重启就走运行时的门：
  容器内 `scripts/set_vln_instruction.sh "go to the chair and stop."`（只入队、立刻返回）。
- **下一步怎么判**：dry-run 起栈 + `scripts/watch_vln_chain.py` 看 `last_note`——
  出现 `discrete_action[1]` 或轨迹类结果 = 指令语义就是主因，实锤；仍旧 `[0]: stop` =
  转去查画面（相机被挡/朝向不对）。

---

## 2026-10-16 · 模型"无限给 0"：不是链路坏了，是逐帧问答 + 车不动 = 答案也不会变

现场从 14:49 起 live 跑了一轮，用户看到模型一直在给 `0`。核查结论：

- **0 就是我们这边的"停"**：`parse_actions` 把文本里的 `STOP` 抠成 `0`，`select_discrete_action`
  对全 0 列表返回 `DISCRETE_STOP`，`discrete_to_velocity(0)` → `VelocityCommand(0,0)`，
  `last_note` 写 `discrete_action[0]: stop`。底盘侧对得上：`ranger_chassis.log` 里
  14:52–14:56 反复 `sustained zero command: steering returned to neutral Ackermann mode`。
  链路没问题，模型确实在说"停"。
- **为什么"无限"**：`internvla_n1_agent_realworld.py` 的 `step_s2` 在每次 non-`look_down`
  调用开头都 `self.conversation_history = []`、`self.past_key_values = None`，然后拿
  "指令 + 8 张等间隔历史帧 + 当前帧"重拼一次 prompt，且 `do_sample=False`（贪心）。
  ⟹ 模型答案 = **f(指令, 当前帧, 本集历史帧) 的确定性函数**。车不动 ⇒ 画面不变 ⇒
  同一帧永远回同一个答案。之前"无限 5"是同一个机制，不是随机故障。
- **5 变 0 只可能是三项输入变了**：指令（14:44 有人设过 `walk forward continuously, do not stop`，
  14:49 重启后回到默认 `Walk forward along the path`）、画面（live 里车真动过）、
  历史帧采样（`np.unique(np.linspace(0, episode_idx-1, 8))`，episode 越长铺得越开，
  而客户端换指令/连续失败都会 reset，每轮 episode 长度都不同）。
- **一个会误导人的坑，记下来**：`[5]`（低头）和 `[0]`（停）在**车这一侧完全同形**——
  都是 `vx=0/wz=0`，底盘日志、`ros2 topic echo /cmd_vel` 都分不出来。
  要看只能看 `/ranger/vln/status` 的 `last_note`（`discrete_action[0]: stop` vs
  `discrete_action[5]: camera-only plan (5/9)`），或者模型端的原始文本。
- **顺带修正本仓之前的一条结论**：本文件里 2026-10-16 那条写"服务端在模型答 `[5]` 时会带
  `look_down=True` 重问一次"——那是**公开仓的形状**。部署在 AGX 的那份（VSCode 本地历史里
  的 `http_internvla_server.py` 快照）里 `look_down = False` 是写死的，**没有 `[5]` 重问**。
  10.0.105.11 那台是哪一份，本机无法确认。

---

## 2026-10-16 · "模型推理正常但车不动"：是 dry-run 生效，不是故障

14:29 起的那轮栈，模型端日志正常、车一动不动。查了三份日志，结论是**设计如此**：

- `internvln_navigation.log`（14:29–14:36）：450 条 info、1 条无关警告，**零错误**；
  `dry-run prediction` 状态分布 `active_plan 323 / stale_plan 36 / no_plan 3`，
  最后几拍稳定 `vx=0.200, wz=0.000, state=active_plan, episode=2`——**计划一直在正常回来**。
- 同一个日志启动那一行就是 `InternVLA active: mode=dry-run`，
  而 `internvln_navigation/ros_node.py:362` 在 dry-run 下算完直接 `return`（只写日志 + 把
  executed_command 记成 ZERO），**根本不碰 `/cmd_vel` 两个发布器**。
- `ranger_chassis.log` 从 06:29:51Z 的 `init complete: odom=/odom twist_in=/cmd_vel` 之后
  一行都没有——底盘侧确实一次速度都没收到，两边对得上。

`robonix_manifest.yaml:141` 是 `mode: dry-run`（11:24 按"人在外面、不许让它动"的要求改的），
14:29 重启后生效。**想让车动 = 改回 `mode: live` + 重启栈**，这是会真发速度的动作，等人到现场做。

顺带两点观察（切 live 前值得先看）：一是 36 拍 `stale_plan`（计划过期后 vx 归零），
真跑起来会是**走走停停**而不是匀速；二是 14:31 有人从 MCP 把指令改成了"往前走，前往前方开阔地"，
指令链路是通的。

---

## 2026-10-16 · 查清 HTTP 500 从哪来：服务端有一处坐标解析缺长度判断

### 起因

现场日志里 36 次"帧失败"，其中 **32 次是 `HTTP 500`**，每次失败还会让旧客户端把服务端的
episode 历史清空。要判断这个 500 是不是本仓客户端发坏了。

### 依据：源码在本地，逐行看的

- 服务端源码地址（两份部署文档都指向同一个仓）：`https://github.com/InternRobotics/InternNav.git`，
  已拉到 `/mnt/ssd/wxf/jiaofu/internVLN/InternNav`（HEAD = `7a5c624`，2026-03-10；仓从 3 月起没动）。
- 现场跑的就是 `scripts/realworld/http_internvla_server.py` + `internnav/agent/internvla_n1_agent_realworld.py`，
  另外打了本地补丁：**缺 `--plan_step_gap`、warmup 的 dtype、depth 改可选**——这三条写在
  `InternVLA-N1 AGX Orin 推理部署记录.md` §7.2 里。**该节第 2 条被粘贴糊掉了**（内容变成一串路径），
  所以"本地还改过什么"文档里缺一条。

### 病根（代码级）

`internnav/agent/internvla_n1_agent_realworld.py:241-243`：

```python
if bool(re.search(r'\d', self.llm_output)):          # 回答里"含数字"就走坐标分支
    coord = [int(c) for c in re.findall(r'\d+', self.llm_output)]
    pixel_goal = [int(coord[1]), int(coord[0])]        # ← 只找到 1 个数字就 IndexError
```

**只要模型这一帧的回答里有数字、但不足两个，`coord[1]` 就 IndexError，未捕获 → Flask 直接 500。**
本地照抄这三行验过（`前方 1 米处左转`、`Move forward 2 meters` 这种都炸；`↑↑↑↑`/`STOP` 不炸；
`(320, 240)` 正常）：

- 崩点**在模型生成之后**（`model.generate` 在其上方），所以失败的请求照样花掉 3~8 秒——
  和日志里失败间隔 ≈ 8 秒吻合；
- 下一次回答换个说法就好了，所以是"时好时坏、自己会恢复"，也不受 reset 影响；
- 客户端无解，只能重试。真正的修复是服务端那一行加长度判断（<2 个数字就退回按箭头解析）。

### 同一条日志里另外两种失败（不是 500）

| 次数 | 报错 | 含义 |
|---|---|---|
| 2 | `trajectory[1][0] must be a finite number` | 服务端返回 **NaN 轨迹**（S1 DiT 数值不稳），本仓客户端正确丢掉 |
| 1 | `discrete_action must not be an empty array` | 模型既没吐箭头也没吐数字，服务端回了 `[]` |

**已排除**：缺 depth 不是病因——`nextdit_async` 的 S1 分支根本不读 `depths_dp`
（`internvla_n1.py` 里只有非 async 分支用它），零深度也不会导致 NaN。

### 还没坐实的

上面这条是**代码级最可能**，不是服务端亲眼见过的 traceback。一锤定音只需一样东西：
**服务端终端那次 500 的报错栈**（`--log-file logs/server.log`）。若是
`IndexError: list index out of range` 且指向 `internvla_n1_agent_realworld.py:243`，则结案。
备选（同样只可能崩在生成之后）：`generate_latents` 在 `--device auto` 双卡分片下的
device/shape 报错、以及那两张 4090 被别人占用导致的 CUDA OOM。

---

## 2026-10-16 · 发原图：本体不再裁剪，交给服务端 resize（+ manifest 切回 dry-run）

### 起因

"请求图片用原图（怎么请求模型端服务的严格参照 go2 的项目来）"。上一节把"图和请求格式不是病因"
当作已排除项，但那只是说**它们不是这次不动的直接原因**，不等于它们和参考实现一致——
翻官方部署文档才发现，**裁剪这件事是凭空加的**。

### 依据（都写在文档里）

- 部署文档 §7 参数表：`--resize_w / --resize_h` = "**输入图像缩放**｜DualVLN 用 384×384"。
  是 **resize**，从头到尾没有"裁剪"二字，也没有任何本体侧几何要求。
- 部署文档 §9 的验证脚本（官方自己给的客户端写法）：
  `files={'image': ('rgb', buf, 'image/jpeg')}` —— 相机整幅直接发。
- 参考实现 `skill-internvla-navigation-rbnx`：取图走原语 `camera/snapshot`，不做任何缩放裁剪。
- 只有本仓的 `image_preprocess.py` 在本地把 640×480 裁成 384×384（切掉约 25% 宽度）。
  它的 docstring 自称"mirrors OpenBMB's Go2 client"，**这句话在两份本地文档里找不到出处**。

结论：**默认改成原图直发**，旧的裁剪模式保留成可选值（它是唯一在本体上跑过的另一种取景，
留着是为了能对照复现，也免得以后又有人凭印象改回去）。

### 顺带一条：图片字段的文件名不用改

参考实现发 `files={"image": ("rgb.jpg", ...)}`，官方文档 §9 发 `('image', ('rgb', ...))`，
本仓发 `('image', ('rgb', ...))`。**文件名（`rgb` / `rgb.jpg`）不参与服务端取件**——
Flask 只看**字段名** `image`，两边字段名一致。所以这条**刻意不改**，跟官方文档保持一致。

### 协议差异：这一条改不了，需要 AGX 那边先换服务器

参考实现（Go2）的请求逻辑是**四接口 + task 会话**：

```
GET  /health                              → server_ready
POST /task/start  {client_id, instruction} → task_id
POST /eval_dual   {client_id, task_id, frame_id} + image
POST /task/report {task_id, status, message}
```

它需要 AGX 上跑的是**新版服务器**（`运行指令参考.md` 里那条）:

```bash
python -m scripts.realworld.server --task-source http --image-source client --plan_step_gap 4 ...
```

而**现在跑在 `10.0.105.11:5801` 上的是旧版** `http_internvla_server.py`。现场实测：

```
/health        GET=404 POST=404
/task/start    GET=404 POST=404
/task/report   GET=404 POST=404
/eval_dual     GET=405 POST=400     ← 只有这一条存在
```

旧版根本没有那三个路由，每帧靠 `json={"reset":bool,"instruction":str}` 维持 episode。
**所以这一条"照 Go2 改"要分两步**：AGX 先按上面那条命令换成新服务器，本体侧才能改客户端协议；
在那之前改客户端等于每个请求都 404。已记在下面"还没解决的"，等确认。

### 设计取舍

- **默认 `full_frame`，但两个模式都能选**（`model_input_mode`）。切换取景是"不报错但行为变差"
  的那一类改动，必须是一个显式配置项，不能靠改代码。
- `model_crop_size: 384` 保留且仍被一致性检查锁在服务器的 `--resize_w/h` 上：
  它在 `full_frame` 下不参与取图，但两个模式的值不能各说各话。
- **没动服务端**：resize 是它的事，本仓仍然只发一张 JPEG。

### 修改记录

| 文件 | 改动 | 说明 |
|---|---|---|
| `packages/internvln_navigation/internvln_navigation/image_preprocess.py` | 修改 | 新增 `prepare_full_frame()` 与分发函数 `prepare_model_input(mode=...)`；模块 docstring 改写成"原图是默认、裁剪是历史模式"，并**删掉那句没有出处的 mirrors OpenBMB 说法** |
| `.../core.py` | 修改 | `MODEL_INPUT_MODE` 默认改成 `"full_frame"`；新增 `MODEL_INPUT_MODE_CENTER_CROP` / `VALID_MODEL_INPUT_MODES`；模式校验从"只能是 center_crop_height"改成二选一，报错信息跟着改 |
| `.../ros_node.py` | 修改 | `_on_image()` 改调 `prepare_model_input(..., mode=config.model_input_mode, ...)` |
| `config/internvln_navigation.yaml` / `robonix_manifest.yaml` | 修改 | `model_input_mode: center_crop_height` → `full_frame`，注释写明依据（resize 不是 crop、两个客户端都发整幅） |
| `tests/test_image_preprocess.py` | 修改 | 新增 `FullFrameTests`：原图**原对象直通不拷贝**、几何报 `(640, 480, 0, 0)`、坏帧仍拒、分发函数两种模式都对、未知模式报错 |
| `tests/test_core.py` | 修改 | 默认值断言改 `full_frame`；`center_crop_height` 仍可选、`stretch` 仍报错 |
| `tests/test_static_contract.py` | 修改 | 打包配置断言改 `model_input_mode: full_frame` |
| `docs/ARCHITECTURE.md` | 修改 | 服务端参数那节：`--resize_w/h` 的说明从"必须和本体裁剪一致"改成"服务端 resize，本体默认原图直发" |
| `robonix_manifest.yaml:141` | 修改 | 调试时留下的 `mode: live` → **`dry-run`**（用具里的 `scripts/set_vln_mode.sh dry-run`）。这条让 `test_set_vln_mode` 的 5 个用例一直是红的，与代码无关 |

### 源码核实（`InternRobotics/InternNav` 拉下来逐行看的，2026-10-16）

上一节有几个说法是**从文档推测**的，源码看完要更正，一并记在这里免得越传越歪。

1. **4 步数组的来头不是 `--plan_step_gap`。** `parse_actions()` 拿正则把模型输出文本里
   的每个箭头都抠出来（`↑/←/→/↓/STOP` → `1/2/3/5/0`），所以 `[2,2,2,2]` 就是模型自己
   吐出的一个动作块（论文：chunks of at most four actions）。`--plan_step_gap` 管的是
   快系统多久重跑一次 S2，跟数组长度无关。**上一节和 `docs/ARCHITECTURE.md` 里那句
   "服务器是 --plan_step_gap 4 所以回 4 步"已就地改掉。**
2. **服务器把收到的图直接 `image.resize((384, 384))`**（`internvla_n1_agent_realworld.py`
   `step_s2`），**没有裁剪**。这就是"发原图"的依据坐实：我们以前本地裁掉的左右各 25%，
   服务器本来会拉伸用上。
3. **历史就是服务端那个 `rgb_list`。** 每次 S2 调用：
   `history_id = np.unique(np.linspace(0, episode_idx-1, num_history))` —— 从**整段 episode**
   里等间隔抽 8 帧，加上当前帧一起喂给模型；`reset()` 清空它。所以 `reset` 频繁 = 模型
   每次只能看到一两帧，等于没有上下文。上一节说的"越清史越不动"，机制就在这里。
4. **"低头(5)"是服务端自己处理的一次重问，不是摄像头动作。**
   `http_internvla_server.py`：模型答的正好是 `[5]` 时，服务端**立刻带着 `look_down=True`
   再问一次**，把**第二次**的答案返回给我们（`look_down=True` 只是把**同一张图**再塞一张
   给模型、并把上一次的回答放进对话历史，画面不动、也没有任何云台指令）。
   推论：**`[5]` 能到本体，说明服务端已经重问过一次、模型还是想低头**——这时唯一的正解
   就是保持不动、换新的一帧再问，也就是现在代码做的事。
   `[5]` 之外含 5 的块（如 `[5,1,1,1]`）**不会**触发这次重问，会原样返回：跳过 5 执行 1
   是唯一讲得通的解释。
5. **官方客户端（部署文档第 8 节那个 Go2 客户端）怎么执行动作**：把整块**累积成一个目标位姿**
   （`incremental_change_goal`：0 不动、1 沿当前朝向 +0.25 m、2/3 转 ±15°），再交给 PID
   追这个位姿；每来一次新回答就更新目标。`[5]`/`[9]` 这两种回答**整条忽略**。
   → 它同样**把块里的 0 当"这一步不动"**，同样忽略 5/9。**这次改的语义跟官方客户端一致**，
   差别只在粒度：官方是把整块摊成一个连续目标，我们是每 tick 走一步。
6. **一个地雷：GitHub HEAD 版的服务器 `request.files['depth']` 是无条件取件**
   （`image`、`depth`、`json` 三个字段都要，depth 要 16 位 PNG）。现场实测**当前跑着的
   服务器不要 depth**（只发 json+image 也 200，见下），所以它跟 HEAD 不是同一版；
   但**如果哪天 AGX 换成 HEAD 那份代码，我们的客户端会每个请求都 400**。
   要换服务器的话，客户端得同时补一个 16 位 PNG 的零深度占位。

### 现场实测（对着 `10.0.105.11:5801` 发真相机原图）

```
A 只发 json+image（本仓现状）           HTTP 200  {"discrete_action":[2,2,2,2]}
B json+image+depth(16bit png)（官方客户端）HTTP 200  {"discrete_action":[2,2,2,2]}
C 原图整幅 + 无 depth，紧接一帧          HTTP 200  {"discrete_action":[3]}
```

三件事同时被证实：**服务器不要 depth**、**原图整幅能正常出动作**、
**`reset` 之后能继续同一段 episode**。（图用的是本地那张真相机原图 640×480。
服务器不记 client id，这几次请求会进它自己的帧历史，`reset: true` 已把那一段清干净。）

### 验证

```
$ bash scripts/set_vln_mode.sh dry-run
changed:live->dry-run
  141:      mode: dry-run

$ bash tests/run_offline_tests.sh
Ran 168 tests ... OK          ← 之前 164 个里有 5 个红（live 导致），现在全绿
$ bash scripts/validate_offline.sh
offline validation OK
```

**已经跑过**：上面那次真实服务器 `POST /eval_dual`（见"现场实测"）——**原图整幅确实能出动作**。
**没有跑过**：真机 / 相机 / CAN / 底盘。也就是说"取景这么发服务器认"已经验了，
"发出去的动作能让车按预期走"还没验，后者要上车。

---

## 2026-10-15 · `discrete_action` 按整条计划解析 + 单帧失败先重试：修"反复返回 0/5 导致原地不动"

### 起因

在 Robonix client 发指令后车不动，`/eval_dual` 反复返回 0 或 5。
对照 Go2 参考实现 [`syswonder/skill-internvla-navigation-rbnx`](https://github.com/syswonder/skill-internvla-navigation-rbnx)
的请求与动作逻辑逐条比对后，定位到**两处客户端偏差**（协议和图像都不是病因）。

### 对比结论（先排除的，再确认的）

| 项 | 参考 skill（Go2） | 本仓（改动前） | 结论 |
|---|---|---|---|
| 服务器协议 | 四接口 `/health` `/task/start` `/eval_dual` `/task/report` | 只有 `/eval_dual` | **非病因**：真服务器实测 `/health` `/task/start` `/task/report` 全 404，本仓对上的是它真正有的那条 |
| multipart 体 | `requests` 拼（`data={"json":...}` + `files={"image":("rgb.jpg",...)}`） | 手拼 | **非病因**：离线拆包对比，字段名 `json`/`image`、content-type、json 内容完全等价 |
| 发什么图 | 原图整幅，让服务端自己 resize | 本体先裁 384×384 方图 | **未验证**，没改（见下"还没解决的"） |
| `discrete_action` | 整条列表都要读：跳过 0/5/9；全 0 = 到点；只有 5/9 = 等一帧 | **只取 `[0]`** | **病因之一** |
| 单帧失败 | 抛异常、任务失败 | 记一次失败 **并重新武装 `reset: true`** | **病因之二** |

两条怎么合起来造成"站着不动"：服务器是 `--plan_step_gap 4`，每帧回的是 **4 步的计划**
（`{"discrete_action": [0, 1, 1, 1]}` 这种很常见）；旧代码只看 `[0]`，
**列表以 0 开头就整个 `plan_hold_s`（1.5 s）刹车**——而 0 在列表里的本义是"这一步不动"，
不是"到了"。同时实测真服务器（`10.0.105.11:5801`）**约 1/4 的请求回 HTTP 500**，
旧代码一次失败就 `reset: true`，于是**大约每 4 帧就把服务端那 8 帧历史清空一次**；
历史越清越空，模型越倾向于答 0 / 5。两个偏差互相喂：越不动越清史，越清史越不动。

### 设计取舍

- **一 tick 只执行"第一个可执行步"，不把 4 步一次跑完。** 参考实现里
  `_chassis.move()` 是阻塞的：它拍一张照、推一次，然后把 4 步**连着做完**再重新拍照。
  本仓不是那个结构——异步 worker 持续重推、每帧都在发新请求、`plan_hold_s` 到点即
  把命令归零。硬塞 4 步进一个 tick 会变成"1 m / 6 s"的运动，而且会和下一份计划打架。
  所以这里只借它的**列表语义**（跳过无效步、全 0 才算停），执行粒度仍是一步。
  这也算给上一节踩坑 5 那个悬着的问题一个答复：**不一次吃 4 步**，列表只用来跳过无效步。
- **全 0 和"含 0"必须分开。** 全 0 = 模型说到点了 → 停；`[0, 1, 1, 1]` = 这一步空着、
  后面要走 → **走**。这一个判断就是本次修复的核心。
- **只有 5/9 的计划 = 明确"保持不动"并写进日志**，不再只说一句 `no-op action 5`。
  5 是"低头看一眼再重推"，Ranger 没有这个俯仰自由度，能做的只有等下一帧——
  和参考实现的 `wait` 一致。日志里带上整条列表，否则"站着不动"看起来像客户端掉线了。
- **重试用的是同一帧、同一 `reset`。** 换 `reset` 重试等于把历史清两次。
  一次重试就把"一次 500 就清史"的概率从 ~25% 降到 ~6%（0.25²）。
- **未知动作码（比如 4）仍然报 `ProtocolError`**，与参考实现一致：契约漂移要吵，
  不能悄悄当成 no-op。
- `failure_retries` 默认 1，设 0 即回到旧行为（想复现旧症状用得上），上限 5。

### 修改记录

| 文件 | 改动 | 说明 |
|---|---|---|
| `packages/internvln_navigation/internvln_navigation/action_adapter.py` | 修改 | 新增 `select_discrete_action()`（照 `discrete_to_commands` 的语义：跳过 0/5/9 → 取第一个可执行步 → 全 0 才算停 → 只有 5/9 返回 `None`）和 `_describe_actions()`（日志里同时给出选中步和整条列表）；`parse_plan()` 不再只看 `[0]`；模块 docstring 补上列表语义 |
| `packages/internvln_navigation/internvln_navigation/core.py` | 修改 | `RuntimeConfig` 新增 `failure_retries: int = 1`（`_bounded_int`，0..5） |
| `packages/internvln_navigation/internvln_navigation/http_client.py` | 修改 | `evaluate()` 改成"同帧重试 `failure_retries` 次再认输"；`_fail()` 只在重试用尽后触发（docstring 同步）；快照新增 `retries` 计数；成功的计划若靠重试救回，`last_note` 标 `(recovered on retry N)` |
| `packages/internvln_navigation/config/internvln_navigation.yaml` | 修改 | 补 `failure_retries: 1`（`test_static_contract` 要求配置与 dataclass 字段一一对应） |
| `robonix_manifest.yaml` | 修改 | 同上，`fault_after_failures` 下面加 `failure_retries: 1` 及注释 |
| `packages/internvln_navigation/tests/test_action_adapter.py` | 修改 | 新增 7 个用例：前导 0 不再刹车、全 0 才是停、只执行第一个可执行步（转角 = 一步的量）、相机动作后面的步能找到、只有 5/9 → hold 且日志带整条列表、`[0, 5]` 不算停、列表里任何位置出现未知码都是协议错误 |
| `docs/ARCHITECTURE.md` | 修改 | 动作码表后面补"4 步计划怎么读"四条；`reset` 那条改成"一帧重试用尽仍失败"后 |
| `packages/internvln_navigation/tests/test_http_client.py` | 修改 | 新增 3 个用例（瞬时 500 被重试救回且不清史 / 重试用尽才清史且计数 / `failure_retries: 0` 回到一帧一请求）；三个"按帧计数"的老用例显式加 `failure_retries: 0`，避免靠 `FakeTransport` 抛错来凑数 |

### 踩坑记录

- **0 在列表里不是"停"。** 只有整条列表都是 0 才是"到了"。把一个 4 步计划的 `[0]`
  当 STOP，是"指令收到了、车不动"这个症状最省事的解释，也是这次的主修复点。
- **失败要重试，但 `reset` 不能跟着抖。** 一次 500 就清史，等于让车在最需要历史的时候
  失忆。重试必须同帧同 `reset`。
- **"只取 `[0]`"当初是刻意选的**（见本页 2026-10-14 那节的踩坑 5：一次吃 4 步是控制策略、
  要人来定）。现在定了：**一步一 tick，列表只用来跳过无效步**——理由写在上面"设计取舍"。

### 验证

```
$ bash tests/run_offline_tests.sh        # 包内
Ran 164 tests ... FAILED (failures=5)
```

新增的 10 个用例全绿。**剩下 5 个红的是 `test_set_vln_mode` 的老问题，与本次改动无关**：
那些用例把 `robonix_manifest.yaml` 复制到临时目录再断言 `dry-run` 行为，而现在
`robonix_manifest.yaml:141` 还停在调试时的 `mode: live`，于是它们报
`already live; nothing to change`。切回 dry-run 即恢复全绿（这条要人点头再动）。

**没有跑过**：真机 / 相机 / CAN / 模型服务。这次改的是"怎么读模型的回答"和"失败怎么记账"，
**没有新增任何能让车动起来的东西**。

### 还没解决的

1. **`384×384` 裁图 vs 参考实现的原图整幅**：这是唯一还没排除的客户端差异。
   本来要做的真机 A/B 没做成——那会儿 internVLN 栈没起（容器里没有相机驱动、
   没有 `internvln_navigation`、没有 zenoh 路由，`ros2 topic list` 只有 `/rosout`），
   A/B 脚本抓 0 帧。要真机验证得先把栈起起来。
2. **如果日志里长期全是"只有 5/9"的计划**，那就是模型一直想要低头看一眼这个自由度，
   客户端无解，得去服务端那边看能不能关掉自动视角调整。届时看
   `/ranger/vln/status` 里的 `last_note` 即可分辨：`camera-only plan ... holding still`。

---

## 2026-10-15 · 新增 `scripts/set_vln_mode.sh`：模式切换走脚本，不再手改 YAML

### 起因

"切换 live / dry-run 支持通过脚本进行修改"。原先只能手改
`robonix_manifest.yaml:141` 那一行，改法靠人记，改错了也没有任何提示。

### 设计取舍

- **只做编辑，绝不起停任何东西。** 脚本改完只把下一步命令**打印**出来。
  武装这个动作本身必须由人来按（`START` 不在脚本能力范围内）。
- **`live` 必须带 `--yes`；`dry-run` 不用。** 危险方向要一个明确动作，安全方向不该有摩擦。
- **导航栈还在跑就拒绝武装**（退出码 4）。一个 CAN 只能有一个主人，这条以前只写在文档里。
- **`mode:` 出现不止一处就硬报错**（退出码 1），不猜哪个是开关——`start.sh` 的 grep
  只会看见第一条，猜错就是拿另一个值去武装。
- **不动包内 `config/internvln_navigation.yaml` 的 `mode:`**：部署这条路根本不读它
  （见本页上一节），改了只会让两个文件互相矛盾；它留在 `dry-run` 正是
  launch 那条路该有的安全默认值。
- 原子写回（临时文件 + `os.replace`），缩进 / 行尾注释 / 文件权限位都保留。

### 修改记录

| 文件 | 改动 | 说明 |
|---|---|---|
| `scripts/set_vln_mode.sh` | **新增** | `status`(默认) / `dry-run` / `live --yes`；退出码 0 成功、1 manifest 有歧义、2 用法、3 缺 `--yes`、4 导航栈在跑。`ROBONIX_MANIFEST` 可覆盖路径（测试靠它） |
| `packages/internvln_navigation/tests/test_set_vln_mode.py` | **新增** | 9 个用例，全部以子进程跑真脚本、只动**临时副本**；每个用例前后对真 manifest 做哈希比对，跑测试本身绝不可能武装机器人 |
| `scripts/set_vln_instruction.sh` | 修改 | **补执行位**（0644 → 0775）。文档一直是 `scripts/set_vln_instruction.sh "..."` 直接执行，没有执行位就是 `Permission denied`——见踩坑 15 |
| `packages/internvln_navigation/tests/test_static_contract.py` | 修改 | 新增 `OperatorScriptTests`：文档里"直接执行"的脚本必须有执行位，且文档确实还那么写（防止测试断言一个化石） |
| `docs/COMMANDS.md` | 修改 | §8.1 改成脚本用法 + 四种拒绝的对照表（sed 降级为"万一脚本用不了"的兜底）；§8.2/§10 改用脚本；§12 排障加三行"切 live 被拒" |
| `docs/RUNBOOK.md` | 修改 | 第 6 步第 2 条改成脚本，并补上 status/回切；速查表加一行 |
| `docs/DEPLOY.md` | 修改 | §9.2 第 2 步、§10 回滚、§12 一页速查都改用脚本 |
| `docs/SAFETY.md` | 修改 | 规则一加"用脚本别手改"；规则二说明 `--yes` 与"导航栈在跑会被拒"；§7 上电前检查加一条 status |
| `README.md` | 修改 | 快速开始加一句怎么切；红线 1 改成脚本命令 |

### 踩坑记录

13. **`pgrep -f` 会认错人。** 第一版用 `pgrep -af 'ranger_base_node|velocity_guard'` 判断
    导航栈在不在跑。**任何命令行里只是提到这两个词的 shell 都会命中**——
    包括 `bash -c '... ranger_base_node ...'` 这种包装层。实测时脚本被我自己的
    包装 shell 骗了，报"导航栈还在跑"并拒绝武装一个完全空闲的机器人。
    改法：先把**本进程的祖先链**（沿 `/proc/<pid>/status` 的 `PPid` 往上走）收集起来跳过——
    真正的底盘进程不可能是"决定要不要武装"这个脚本的祖先。宁可漏报也不误报，
    因为误报一次这个检查就没人信了。
14. **"已经是目标模式"会在校验之前短路。** 第一版把"取当前模式 → 比对 → 相同就直接退出"
    放在最前面，于是喂一个含**两个** `mode:` 键的 manifest 时，只要 grep 到的那个
    恰好等于目标值，就直接回"already live"，歧义完全没被发现。
    改成**任何动作（包括 `status`）之前先做唯一性校验**。
15. **`set_vln_instruction.sh` 一直没有执行位。** 本仓的脚本习惯是 `bash scripts/x.sh`，
    但**这个脚本三份文档里都写成 `scripts/set_vln_instruction.sh "..."`（直接执行）**，
    而文件的权限是 0644。现场照着文档敲就是 `Permission denied`。
    加执行位修掉，并加了 `OperatorScriptTests` 把"文档直接执行的脚本必须有执行位"钉住——
    顺带要求文档里确实还是那么写的，否则就是在断言一个已经不存在的用法。

### 验证

```
$ bash scripts/validate_offline.sh
Ran 155 tests ... OK          # 144 → 155，新增 11 个（模式切换 9 + 契约 2）
manifest consistency OK
offline validation OK

$ scripts/set_vln_mode.sh
mode     : dry-run            # 真 manifest 未被动

# 在临时副本上逐条验过：
live --yes            -> rc=0  只改了第 141 行，权限 644 保留，行尾注释保留
dry-run               -> rc=0
dry-run（再来一次）    -> rc=0  "already dry-run"
live（不给 --yes）     -> rc=3  文件未动
两个 mode: 的 manifest -> rc=1  文件未动
假的 ranger_base_node  -> rc=4  文件未动；杀掉后 rc=0
真 manifest           -> md5 前后一致
```

**没有跑过**：真机上的武装流程。脚本已就位，但 `mode` 仍是 `dry-run`，
第 8、9 节的实操步骤仍待你在现场执行。

---

## 2026-10-15 · 新增 `docs/COMMANDS.md`（命令速查），并查清"哪个配置赢了"

### 起因

"怎么切 live、怎么实机验证、把命令汇总一下"。原来的命令散在 4 份文档里：
`DEPLOY.md` §12 有一页速查但太简（没有期望输出、没有判据），
`RUNBOOK.md` 末尾那张表又没有顺序。汇总成一份独立文件。

### 顺手查清的两件事

1. **`mode` 由 manifest 说了算。** 追问"改哪个文件的哪一行"时把这条链路走了一遍：
   包自己的 `packages/internvln_navigation/start.sh` 是
   `exec python3 -m internvln_navigation.provider`——**不传任何 yaml**；
   `provider.py:129` 的 `RuntimeConfig.from_mapping(config)` 拿的是
   robonix 从 **manifest** 的 `config:` 块递进来的字典；
   `start.sh` 的 `stack_mode()` 也只 grep manifest。
   → 所以**包内 `config/internvln_navigation.yaml:15` 那个 `mode:` 在部署这条路里不生效**，
   只在用 `launch/internvln_navigation.launch.py` 单独跑包时才生效。切 live 只改 manifest 一行。

2. **`max_vx` 的悬案结了：运行时是 0.25（manifest），不是 0.15（包内 yaml）。**
   同一条推理。`ACCEPTANCE.md` 末尾原先挂着的"⚠️ 包内 max_vx 默认 0.15"是对的，
   但没说清部署下到底哪个赢——现在明确了：走 manifest 就是 0.25，
   和 `guard_max_vx_mps` 一致，**不会二次截断**。

### 修改记录

| 文件 | 改动 | 说明 |
|---|---|---|
| `docs/COMMANDS.md` | **新增** | 0~13 节：路径总表 → 起容器 → 构建 → 离线自检 → 探模型 → dry-run → 切 live → **实机验证 6 段顺序** → 收工回滚 → 三层急停 → 排障 → 禁忌。每步都带"期望输出" |
| `docs/DEPLOY.md` | 修改 | §12 顶部加指路 + 镜像 tag 覆盖提醒；速查里的 `run_container.sh vln` 补上 `ROBONIX_VLN_IMAGE=...` |
| `README.md` | 修改 | 文档表加 `COMMANDS.md`（原来 `ACCEPTANCE.md` 也不在表里，一并补上） |

### 踩坑记录

11. **`run_container.sh vln` 的默认镜像 tag 在本机不存在。** 默认要
    `robonix:humble-orin-deps-20260821`，本机 `docker images` 里只有
    `robonix:humble-orin-deps-slim`(19GB)、`robonix:humble-orin-follow`(21.9GB) 及其 `.bak-*`。
    硬跑会 `[fatal] image ... is not on this robot`。用
    `ROBONIX_VLN_IMAGE=robonix:humble-orin-follow bash scripts/run_container.sh vln`。
    `DEPLOY.md` §6.1 早就写了这个兜底，但 §12 的速查没写——速查被抄走就踩。
12. **本机在跑的容器名和脚本建的不是一个。** 脚本建 `robonix_vln_dev`；
    本机现在跑的是**手工起的** `robonix_internvln_dev`（镜像 `robonix:humble-orin-follow`，
    `--init`、`--network host`、挂载三个目录、`c 189:* rmw`，配置与脚本等价）。
    两边都能用，但 `docker exec` 之前先 `docker ps | grep -i vln` 认清是哪个。

### 验证

```
$ bash scripts/validate_offline.sh
offline validation OK            # 全是文档改动，判据不变

$ sed 's/^      mode: dry-run$/      mode: live/' robonix_manifest.yaml | grep -n 'mode: live'
141:      mode: live                # 只命中第 141 行，注释里的 "mode: live"（第 133 行）不受影响
```

**没有跑过**：真机上的完整开机 → 实机验证流程。`COMMANDS.md` 第 8、9 节是
**给人照着做的步骤**，不是我跑过的记录。`mode` 仍是 `dry-run`。

---

## 2026-10-15 · 观察器不再把 "dry-run 不发速度" 说成 "节点没起来"

### 起因

真机上第一次跑 dry-run：`start.sh` 起来了、模型端看得到请求日志、
`/ranger/vln/status` 也每 0.4 s 一条（`frames=180 healthy=True episode=1`），
但 `watch_vln_chain.py` 三行全是 `pub 0` 并写着 **"vln node is not running"**：

```
/ranger/vln/cmd_vel_raw        0   1     0   -   -  no publisher <- vln node is not running
/cmd_vel_nav                   0   1     0   -   -  no publisher <- source mux is not running
/cmd_vel                       0   2     0   -   -  no publisher <- chassis guard is not running
```

节点明明在跑。**是观察器的提示语写错了**：它只按 `pub == 0` 判断，完全不看 `mode`，
于是把 dry-run 的**设计状态**报成了**故障**。文档里 `docs/RUNBOOK.md` /
`docs/DEPLOY.md` 的样例输出更糟——它们画的是 `pub 1 / Δmsg 12 / vx +0.1667` 的
**live 样子**，却把 `mode=dry-run` 写在下面，等于教人按错的判据去验收。

### 修改记录

| 文件 | 改动 | 说明 |
|---|---|---|
| `scripts/watch_vln_chain.py` | 修改 | 表头加 `mode=`；`pub==0` 时若 `mode=dry-run` 改报 `dry-run: no velocity publisher by design`；底部 CHECK 行按模式分两套（dry-run 说"三个 pub 都应为 0，判据看 status"，live 才是原来那三行）；顺手修掉 status 缺失时那句误提 dry-run 的话 |
| `docs/RUNBOOK.md` | 修改 | 第 4 步样例输出换成 dry-run 的**真实**输出（三行 pub 0 + 完整 status 三行）；"要看的"改成看 status（age/msgs/frames/last_note），并写明 `pub=1`/`Δmsg` 是 live 之后的判据 |
| `docs/DEPLOY.md` | 修改 | §8.2 同上：样例换成真实 dry-run 输出，判据表加一行"`pub=1` / `Δmsg` 在涨 —— **切到 live 之后才成立**" |
| `docs/ACCEPTANCE.md` | 修改 | §4 dry-run 加前提说明（这一节只能看 status）；删掉那条**错的**判据"`/ranger/vln/cmd_vel_raw` 的 `Δmsg` 持续 > 0"（dry-run 下永远是 0）；"画面变化→预测速度变化"改成看 `dry-run prediction:` 日志；`superseded` 那句改成实际会出现的原文 `discarded a plan for a superseded instruction` |

### 踩坑记录

7. **`pub 0` 单独看没有意义，必须连着 `mode` 一起看。** dry-run 的设计就是
   "建订阅、调模型、发 status，但**不建任何速度发布者**，guard 也不启动"——
   三个话题 `pub 0` 是这条设计的**证据**，不是故障。判活只能看
   `/ranger/vln/status`（1 Hz，`age` 小、`frames` 在涨）。
   反过来，`/cmd_vel` 上 `pub == 1` 是 **live 的**硬要求；dry-run 下它要是 1，那是**事故**。
8. **`Δmsg` 在 dry-run 下永远是 0**（没人发，哪来的消息）。这条判据属于 live。
   早先文档把它抄进了 dry-run 清单，害人按错的表去验收。
9. **`/cmd_vel` 的 `sub` 计数包含观察器自己**，所以 `sub 2` = 观察器 + `ranger_chassis`，
   说明底盘原语在听着；不能读成"有两个订阅者，出问题了"。
10. **`last_note` 不是"每帧都变"。** 它只在 episode 结束（每集最后一帧）或
    代际被顶掉时才写；每帧的换算结果要看 `start.sh` 终端的 `dry-run prediction:`
    日志（`ros_node.py:363`，最多 1 Hz 一条，虽然 dispatch 是 50 Hz）。

### 验证

```
$ bash scripts/validate_offline.sh
offline validation OK        # 144 tests + 18 manifest 检查全绿
```

**没有跑过**：这次只改了一个观察脚本的**显示文案**和四份文档的**样例输出/判据**，
没碰控制路径，`mode` 仍是 `dry-run`。

---

## 2026-10-14 · 删掉三个不存在的路由，客户端只留 `POST /eval_dual`

### 起因

服务端**只有一个路由**。这一条在两份本地部署文档里都写着，而且当天在真机上又验了一遍：

```
$ curl -sS -D - http://10.0.105.11:5801/eval_dual
HTTP/1.1 405 METHOD NOT ALLOWED
Server: Werkzeug/3.1.8 Python/3.10.20
Allow: POST, OPTIONS
```

而本仓客户端**自己编过** `/health`、`/task/start`、`/task/report` 三个接口，
还配套写了一整套"任务状态机"（`client_id`、`task_id`、`POST /task/start` 开任务、
`POST /task/report` 收尾）。这三个名字：

- 官方 `scripts/realworld/http_internvla_server.py` 里没有；
- `InternVLA-N1-DualVLN-部署文档.md` 里没有（只有 `POST /eval_dual`）；
- `InternVLA-N1 AGX Orin 推理部署记录.md` 里没有（同上）；
- 真机上一次都没通过——**打过去就是 404**。

以本地文档为准 → **删掉**。顺带把围绕着它们的假状态机一起拆了。

### 修改记录

| 文件 | 改动 | 说明 |
|---|---|---|
| `internvln_navigation/http_client.py` | 重写 | 删 `health()` / `start_task()` / `report()` / `_request()` / `_decode_object()` / `IMAGE_FIELD_CANDIDATES` / `_task_id`；只留 `_eval_dual()`。状态机换成 `reset_pending` 一位标志 + `episode` 计数 |
| `internvln_navigation/core.py` | 修改 | `RuntimeConfig` 删 `client_id`、`health_interval_s` 两字段及其校验；`_server_base_url` 注释改为"客户端自己拼它唯一那条 `/eval_dual`" |
| `internvln_navigation/provider.py` | 修改 | 4 处 `task_id=""` 参数删除；`get` 分支 `detail` 改报 `episode=`；文档串"四个 HTTP 调用"→"一次 HTTP 调用" |
| `internvln_navigation/ros_node.py` | 修改 | dry-run 日志 `task=` → `episode=` |
| `internvln_navigation/config/internvln_navigation.yaml` | 修改 | 删 `client_id`、`health_interval_s`；`fault_after_failures` 补注释 |
| `capabilities/lib/internvln_navigation_control/srv/VlnTask.srv` | 修改 | 响应删 `string task_id` |
| `robonix_manifest.yaml` | 修改 | 删 `client_id`、`health_interval_s`（config 现 35 键） |
| `start.sh` | 修改 | `require_internvla_server_url()` 拦 `/eval_dual` 后缀**和**那三个已删路由；`check_internvla_server()` 改成 `GET <base>/eval_dual`，认 **405 + `Allow: POST`** |
| `scripts/probe_internvla_server.py` | 重写 | 删 `--client-id` 与四步流程；只打 `POST /eval_dual` N 帧，逐帧打印 `reset` 与耗时；末尾打印原始响应体 |
| `scripts/validate_manifest_consistency.py` | 修改 | 导入 `IMAGE_FIELD`/`IMAGE_FILENAME`；`_ENDPOINT_SUFFIXES` 仍列四个名字用于**拒收** |
| `scripts/watch_vln_chain.py` | 修改 | 状态行 `task=` → `episode=`；第二行 `image_field=` → `reset_pending=` |
| `tests/test_http_client.py` | 重写 | 新增 `json_part()` 解 multipart；用例改为验 `reset` 时序（首帧 true / 续帧 false / 换指令再 true / 失败后重 true）与"每帧都带指令" |
| `tests/test_static_contract.py` | 修改 | 新增 `DeletedRouteTests`（源码里不许出现那三个字符串、`"/eval_dual"` 只许出现 1 次）+ 配置键/dataclass 双向比对 |
| `tests/test_provider_contract.py` | 修改 | `response.task_id == ""` → `assertFalse(hasattr(response, "task_id"))` |
| `README.md` / `docs/DEPLOY.md` / `docs/RUNBOOK.md` / `docs/ARCHITECTURE.md` / `docs/ACCEPTANCE.md` | 修改 | 四接口表述全部改成"唯一一条 `POST /eval_dual`"；DEPLOY §4 四条 curl → 两条；ARCHITECTURE §2 接口表四行 → 一行、§7 字段名协商整节重写 |
| `packages/internvln_navigation/README.md` / `CAPABILITY.md` | 修改 | 同上 |
| `.env.example` / `.env` | 修改 | 注释改写；`.env` 里那句"大概率是 `--task-source terminal` 起的需要重启"是**错的**诊断，已删 |
| `robonix_manifest.yaml` / `scripts/set_vln_instruction.sh` | 修改 | 注释里残留的 `POST /task/start` → `POST /eval_dual` |
| `docs/RUNBOOK.md` / `docs/DEPLOY.md` / `docs/SAFETY.md` | 修改 | `stop` 的语义：删掉"通知服务器 task 作废"——没有 task 可作废，服务器只缓存帧历史 |
| `docs/CHANGELOG.md` | 新增 | 本文档 |

### 踩坑记录

1. **`reset` 就是全部状态。** 服务器**没有会话、没有 client id、不记你是谁**，
   唯一的状态是它自己缓存的帧历史（`--num_history`）。所以：
   - `reset: true` = 清空历史、开新 episode；`reset: false` = 追加。
   - 什么时候必须带 `true`：**第一帧、换指令后的第一帧、任何一帧失败后的下一帧**。
     最后一条是这次新加的——一旦有一帧没答上来，服务器那段历史就可疑了，宁可清掉重来。
2. **指令每一帧都要带。** 服务器不会替你记"上次说的是什么"，漏了就 500 或退回它内置的默认指令。
   客户端因此把 `instruction` 存在 `_pending_instruction`，每帧都发。
3. **"服务活着吗"没有健康路由可问。** 对一个只收 POST 的路由发 GET，Flask 会回
   **405 + `Allow: POST, OPTIONS`**，这就是判据；`Connection refused` 才是没起。
   `start.sh` 和 `docs/DEPLOY.md` 都按这个写的，别再改回 `/health`。
4. **`image` 字段名从来只有一个。** 早期那个 `IMAGE_FIELD_CANDIDATES`（`rgb.jpg` ↔ `image`）
   加"遇到 4xx 就换一个"的协商逻辑，是在解一个**不存在的问题**：两份本地文档和
   `vln_query.py` 里字段名都是 `image`。副作用是第一次请求必然先撞一次 400。
5. **响应里的 `discrete_action` 是数组，不是标量。** 真机实测：
   `{"discrete_action": [2, 2, 2, 2]}`（4 个 = `--plan_step_gap 4`）。
   当前 `action_adapter.parse_plan()` **只取 `[0]`**，也就是"每步都重新问一次"。
   这是最保守的用法（不会拿着 4 步的旧计划瞎跑），但等于放弃了 `plan_step_gap` 的省算力。
   **要不要一次吃 4 步是控制策略问题，需要人来定，这次没动它。**
6. **405 的响应体是 HTML，不是 JSON；而且它带 `Connection: close`。**
   所以探活只读 `Allow` 头和一个状态码，**不要**去 `json.loads()` 响应体
   （`http_client.py` 里解析 JSON 的是推理响应，不是探活）；`http.client` 用完要
   `finally: connection.close()`，这条连接服务端已经声明不再复用。

### 验证

```
$ bash scripts/validate_offline.sh
Ran 144 tests ... OK
[ OK ] model server: the one route is POST /eval_dual; frame part is 'image' (filename 'rgb')
manifest consistency OK
offline validation OK

$ bash build.sh          # 在 robonix_internvln_dev 里
[internvln_navigation/build] done
# 重新生成的 MCP 绑定里 task_id 出现 0 次，VlnTask_Response 恰好 5 个字段

$ python3 scripts/probe_internvla_server.py --url http://10.0.105.11:5801 --frames 3
POST /eval_dual  x3  instruction='Walk forward along the path'  (66321 B JPEG, field 'image')
    frame 0: ... reset=True (414 ms)
    frame 1: ... reset=False (396 ms)
    frame 2: ... reset=False (420 ms)
PROBE OK: the server speaks the protocol this client expects
  last response body: {"discrete_action": [2, 2, 2, 2]}

$ bash /tmp/vln_check.sh   # start.sh 的预检单独跑
正常 url  -> POST /eval_dual is registered (GET -> 405, Allow: POST, OPTIONS)   rc=0
带 /eval_dual -> 拦住 rc=1
带 /health    -> 拦住 rc=1
端口没人听    -> 拦住 rc=1
```

**没有跑过**：真机上的 `start.sh` / `rbnx boot` / 相机 / CAN。这次改的全是
"怎么跟模型服务说话"这一层，**没有新增任何能让车动起来的东西**，dry-run 仍是默认。