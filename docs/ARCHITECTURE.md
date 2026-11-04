# 架构 · 远端模型服务怎么接进本体项目

这一页回答一个问题：

> 用户给一句话 → 请求远端模型 → 持续拿到动作 → 在 Ranger 底盘上执行。
> 这件事怎么按 Robonix 的模式嵌进来？

---

## 1. 结论先给

| 问题 | 决定 | 为什么 |
|---|---|---|
| 要几个原语？ | **1 个**：`internvln_navigation` | 模型客户端不是独立能力，是这一个原语的内部实现 |
| 要几个服务？ | **1 个（可选）**：`speech` | 语音的 ASR/TTS 只在有人用嘴交互时用；导航本身一个服务都不要 |
| 要几个技能？ | **0 个**（可选 1 个语音入口） | 指令来自 `vln/task` 能力或 ROS 话题，走对话是另一件事 |
| 模型客户端放哪？ | **放在机器人端原语里** | 和 `go2_robottrack` 一个位置，改动面最小 |
| 要不要中间 Flask 转发？ | **不要** | 直接对模型服务说话，动作码换算留在本包里 |
| 怎么保证不停不下来？ | 计划过期归零 + 连续失败撤销计划 | 见 [SAFETY.md](SAFETY.md) |

## 2. 远端接口：只有一个（本体端只调这一个）

这个接口由**算力服务器**提供，本仓只负责按这个协议说话。

> ⚠️ 名字与现状，别照抄旧笔记：官方 InternVLA-N1 仓里那份的文件名是
> `scripts/realworld/http_internvla_server.py`（`scripts/realworld/server.py` 这个名字不存在），
> 而它**只暴露 `POST /eval_dual`**，字段是 `request.files['image']` + `request.files['depth']`
> + `request.form['json']`（`{"reset","instruction"}`），绑 `0.0.0.0:5801`。
> 本仓早期版本自己编过 `/health`、`/task/start`、`/task/report` 三个接口，
> 官方文件和两份本地部署文档里都没有它们，**已删除**。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/eval_dual` | multipart：`json={"reset":bool,"instruction":str}` + `image`（JPEG 文件字段） | `{"discrete_action":[...]}` 或 `{"trajectory":[[x,y],...],"pixel_goal":[...]}` |

这是**唯一**的接口。服务器无鉴权、无会话、不记 client id，除了一段帧历史之外没有任何状态：

- `reset: true` = 清空帧历史、开一个新 episode（第一帧、换指令后、**一帧重试用尽仍失败**后的下一帧都带它；
  同一条指令跑满 `history_reset_frames` 帧时也带一次，但默认 0 = 从不，见下）；
- `reset: false` = 把这帧追加到历史（`--num_history` 那一段）里；
- `instruction` **每一帧都要带**，服务器不会替你记上一次说的什么（服务端读这个字段：
  `data.get('instruction', None) or "<内置兜底句>"`，2026-10-30 在 10.0.105.11 上核实过）。

**中途清历史：默认关闭（`history_reset_frames: 0`）。** 服务器存的是"本 episode 从第一帧起的
所有帧"，每次评估用 `np.linspace(0, episode_idx - 1, 8)` 在这一整段里**均匀抽 8 帧**当上下文
（`internvla_n1_agent_realworld.py:184`）——**训练时就是这么抽的**
（`internvla_n1_lerobot_dataset.py:1004` 同一个 linspace，从第 0 帧起）。所以这 8 帧就是模型
对场景的记忆，也是它"第一次在哪儿看见目标"的唯一记录。官方客户端从头到尾只 reset 一次
（`policy_init` 只在第一帧是 `True`，`http_internvla_client.py:54`）。

2026-10-29 我们加过一个"每 N 帧清一次"的旋钮（当时默认 20，后来 8），理由是"跑久了抽到的
都是转身前的旧画面"。**这个理由是猜的，而且和训练分布相反**：现场 45 次请求里清了 5 次
（`memory_flushes: 5`），等于每 8~10 秒失忆一次。目标一旦离开画面，模型既没有证据"我刚在哪儿
见过它"，也不知道自己已经开过去了——地上看到的就是"锁不住目标 + 开过了还在往前走"。
2026-10-30 起默认 **0 = 永不中途清**。旋钮保留着（写 N 就是每 N 帧一次，状态里记
`memory_flushes`，不算新任务），但那要有一个量出来的理由，不是猜的。

> 活着的判据：对一个只收 POST 的路由发 GET，Flask 回 **405 + `Allow: POST, OPTIONS`**。
> 上车前按 [DEPLOY.md](DEPLOY.md) 4.2 用 curl 确认。

动作码（`discrete_action` 的元素）：

| 值 | 含义 | 本体端换算成 |
|---|---|---|
| 0 | 停 | `(0, 0)` |
| 1 | 前进约 0.25 m | `vx = 0.25 / t`，`t = max(action_step_s, 0.25 / max_vx)` |
| 2 | 左转 15° | `vx = 0`，`wz = +radians(15)/t` |
| 3 | 右转 15° | `vx = 0`，`wz = -radians(15)/t` |
| 5 / 9 | 低头 / 切摄像头 | **空操作**（不是错误）——本机器人没有这些自由度 |
| 其它 | — | `ProtocolError`（宁可报错也不要乱走） |

关键在 `t`：**限速只会让这一步走得更久，不会让它走得更短**。标称时长是
`action_step_s`（1.0 s）；如果 0.25 m/s 超过 `max_vx`，`t` 就按比例拉长，位移仍是 0.25 m。
参考实现走的是底盘闭环位移（Go2 的 `move(forward_m=0.25)` 到点才返回；官方 http 客户端是
PID 追世界系目标位姿）。本仓从 2026-10-30 起也对齐了：`odom_feedback_enabled` 打开时，
一步在 `/odom` 报出"转够 15° / 走够 0.25 m"时就结束，`t` 退化成兜底（见第 6 节）。

`discrete_action` 通常是**一条最多 4 步的动作块**，不是一个码：模型在文本里一次吐出
"↑↑↑↑"这样的动作串，服务端把它找到的每个箭头都变成一个元素
（`internvla_n1_agent_realworld.py` 的 `parse_actions`；论文里叫 action chunk，
"turn chunks of at most four actions"）。`--plan_step_gap` 管的是**快系统多久重跑一次**，
不是这条数组的长度。
本体端按两份**官方实现**读它（Go2 参考仓的 `discrete_to_commands` + `for command in
commands: _chassis.move(**command)`；官方客户端 `http_internvla_client.py` 的
`incremental_change_goal`）：

- 列表里的 0/5/9 **跳过**——开头的 0 是"这一步不动"，不是"到点了"；
- 列表**按顺序**执行，顺序是模型的，截断只从尾部截；
- 一次回答实际执行几步由 `max_chunk_actions` 决定。**默认是 1**（2026-10-29 定）：
  那是一步 0.25 m 或 15°，然后停车、拍新帧、重新决策；
- 为什么不是 4：两份官方实现确实整条跑完，但它们**每一步都是闭环**的（Go2 的
  `_chassis.move(forward_m=…)` 阻塞到走到位才返回；官方客户端是 PID 追一个里程计
  目标位姿）。本栈的每一步是**开环盲走**、也没有避障，4 步 = 每次决策盲走 1 m。
  想换回"整块执行"，把 `max_chunk_actions` 调到 4 即可，机制是现成的；
- **整条列表全是 0** 才是"到点停车"；
- 整条列表只有 5/9 = 模型要一个本机器人没有的自由度，**原地等下一帧**（日志会写明）。

**只有"一直卡在 STOP"才算任务结束：连续 ≥ `finish_after_stops`（默认 20）次 *且* 这一串已经
持续 ≥ `finish_after_stops_min_s`（默认 30 s）。** 0 不是"这一帧没动"，而是模型对"到没到"的
判定；但偶尔一帧 0、或者几帧 0 之后又给出前进，都不该结束任务——短促的 0 可能是画面里目标
刚被挡住、或者一帧坏图。runtime 在每份计划装进 `PlanStore` 之后计数（`VlnRuntime.note_plan`），
两个条件都满足时由 ROS 线程的 `pump()` 做和操作员 `stop` 完全一样的两件事：**退掉计划 +
卸掉 client 的指令**，然后这个进程不再发 `/eval_dual`。`robonix/primitive/vln/task get` 从这时
起回 `status="finished"`、`running=false`，`instruction` 是刚结束的那条任务——这正是 executor
收尾叶子所需要的信号，而不是让叶子一直轮询一辆"还 running 但永远不动"的车。中间只要来一帧
1/2/3 或轨迹，计数和计时一起清零；5/9 以及"0 混着 5/9"都不算 STOP。发新指令（或 `stop`）会
清掉这个完成态，任务重新开始算。

**动作码那条路上，转向是纯原地旋转**（`vx` 恰好为 0）。因为"转 15°"就该是转 15°，
不该带一个前进分量。Ranger 在 `linear.x == 0` 时本来就是原地转，guard 也放行。

### 轨迹那条路：走模型画的弧，不设转弯阈值

模型另一种回包是 `trajectory`（一串 `[x, y]` 点，机器人本体坐标系，+x 前 +y 左）。
这时我们的换算只有一句话：**取第一个至少 `lookahead_m`（1.00 m）远的点，在 `action_step_s`
里既转过去又走过去**——也就是走出唯一那条穿过该点的弧：

```
vx = 前进量 / action_step_s
wz = 2 · vx · sin(方位角) / L        # L = 到选中点的实际距离
（超出 max_vx / max_wz 时，两者按同一个系数缩小，时长按同一系数拉长）
```

**这是标准纯追踪**：`wz = 2·v·sinα / L`，隐含半径 `L / (2 sinα)`——正好是"过这个点、
和当前朝向相切"的那个圆。2026-10-29 之前用的是 `wz = 方位角 / action_step_s`，
那个式子隐含的半径是 `前进量 / 方位角`：只有当选中点正好在两倍前进量远的地方才等于模型的半径，
**正常情况（点就在一个前视距离处）算出来是模型的两倍**——车走的弧比模型画的平一半，
每帧都得靠重拍去追。实测一条 2.86 m 的弧：旧式子命令 5.73 m，纯追踪 3.8 m（取样越远越贴近 2.86）。
旧式子对"取到哪个点"极其敏感（同一条弧能从 2.86 跳到 5.73），这本身就说明它不是曲率估计。

**前视距离要盖过一次"答话间隔"**（2026-10-30 从 0.30 m 改成 1.00 m）。同一个式子，`L`
取小了不会把车带歪，但会让车**追一个已经过去的点**：实测答话间隔中位 1 s、p90 3 s，这段时间
车走 0.15–0.75 m；0.30 m 的前视点在 45% 的帧里已经在车身后方，还有 62% 的帧不到 8 cm——
这就是"目标就在视野里车却走 S 形、最后错过"的来历。1.00 m 盖住了 p90。要点是这**不是转向
增益**：同一条曲线，取远一点读到的方位角更小、弧更平，但半径 `L / (2 sinα)` 仍然对得上。

**钳位是等比例的，不是分轴的**。分轴钳（vx 砍到上限、wz 原样）会让弧变紧：前进少了、
转角没少，车比模型画的线拐得更狠。等比例缩放保持弧的半径不变，只把它开慢一点。

**没有额外的转向增益。** 2026-10-22 曾经试过 `trajectory_yaw_gain` 和
`trajectory_yaw_boost_max_deg`（把方位角乘大），实测转向幅度太大，已经**整段回退**：
现在是纯追踪的换算，不是"把方位角放大"。

**servo 开着时，轨迹还会被折成一个目标点**（2026-10-30）：`PlanStore` 用同一套规则选出被
追踪的那个点，把"到它的位移 + 转过去的角度"叠在**答话那一刻的位姿**上，得到世界系目标位姿，
交给下面的伺服闭环。上面的速度换算只在 servo 关掉时才用。差别是根本性的：一个保持住的
速度永远说不出"到了"，一个地上的点可以。

**路径短于 `trajectory_stop_m`（5 cm）= 模型在说"就在这儿"**：命令发 0，并且当成模型自己
的 STOP 计数（`model_stop`）。这一条是必须的——模型可能只回轨迹不回离散动作，而"连续 N 次
STOP 才算任务结束"的判据只认离散 0 的话，这种模型永远结束不了任务。以前这里是个 1e-6 的
保护，一条 2 cm 的路径会被当成 0.02 m/s 的速度保持整个时长：车"到了"却一直在原地蹭。

**这里没有转弯阈值，这是故意的。** 曾经有个 15° 的门槛，方位角不到 15° 就 `wz = 0` 直走。
后果是灾难性的：模型给的路径近处天生就几乎不偏（它是从当前朝向平滑铺出去的），于是每一帧
都判成"直走"，车就永远直着走、从不拐弯——**阈值把模型唯一的转弯信息整条吃掉了**。现在 3° 的
方位角就是这条弧该有的偏转；真的笔直才算 `wz = 0`（那确实是 0），不是"没到阈值"。

**太大转不过来的才原地转**，而且判据是底盘自己给的：Ranger 在
`|linear.x| / |angular.z|` 小于它的 `min_turn_radius` 时会**默默丢掉 `linear.x`**，
把一个弧线命令变成一次没人要的原地转。所以命令先按底盘上下限钳好，再问一句
"这个弧的半径 ≥ 0.81 m 吗"：够就照着走弧，不够就**明着**下原地转命令，让下一帧从新朝向重推。
本仓这组数字下，分界点在**方位角 ±10.7°**（`asin(0.30 / (2 × 0.81))`，2026-10-29 前印的是
`atan2` 算出的 20.3°，那个式子配的是旧的 wz 公式；纯追踪下弯更"真"，所以更早就该改原地转）；
`scripts/validate_manifest_consistency.py` 会把当前分界点打出来。

服务器侧的关键参数（**在算力服务器上**，不在本仓；以下三个是官方那份脚本真实接受的）：

```
--resize_w 384 --resize_h 384        # 服务端把收到的图缩放到 384x384
--num_history 8                      # 每次评估吃 8 帧历史（服务端自己存）
--device cuda:0 --model_path <权重目录>
```

决定"动作频率上限"的是服务端每次调用里几次真跑 planner，本仓没法从参数推断，
**只能实测**：拿 `scripts/watch_vln_chain.py` 看 `/ranger/vln/cmd_vel_raw` 的 `Δmsg`
涨得多快。本机实测 p50 **0.778 s**、p90 0.915 s（`rbnx-boot/logs/internvln_navigation.log`，
493 份计划），所以 `action_step_s` 取 1.0 s、看门狗 `plan_hold_s` 留 1.5 s。

**本体发什么图**由 `model_input_mode` 决定，默认 `full_frame`：相机整幅直接发，
resize 是服务端的事（文档 §7 就叫"输入图像缩放"，没有裁剪这一步；官方文档 §9 的客户端
和 Go2 参考实现也都是整幅发）。`center_crop_height` 是历史模式——本体先缩到 384 高、
再中心裁 384×384；只有它会用到 `model_crop_size`。
`scripts/validate_manifest_consistency.py` 仍把 `model_crop_size` 锁在服务器的 `--resize_w/h` 上，
两个模式不会各说各话。

## 3. 为什么把客户端放在原语里，而不是中间加一层

参照仓里有一个 `vln_bridge.py`（Flask 转发）。**本仓刻意不要它**，原因有三：

1. **多一个进程就是多一个能死的地方**：Flask 挂了、端口占了、supervisor 没拉起来——机器人停，但你不知道该怪谁。
2. **它解决的是一个不存在的问题**：那个转发是为了适配一个"和官方 server 协议不一样"的模型服务。
   现在直接对官方 server，转发层只剩翻译动作码——那是纯函数，搬进包里就行（`action_adapter.py`）。
3. **它挡住了状态**：`frame_id`、`reset_pending`、失败计数、代际号、episode 号都在客户端里。
   隔一层 HTTP，这些状态要么丢，要么再同步一遍。

所以：**动作码 → 速度的换算成为包内的纯函数**（`action_adapter.py`，有单测，不需要网络），
原语自己持有全部会话状态。

## 4. 换模型时改哪里：`Evaluator` 那个接口

`worker.py`（推理线程）只认一个鸭子类型：

```python
class Evaluator(Protocol):
    def evaluate(self, frame: EncodedFrame) -> InferencePlan: ...
    def close(self) -> None: ...      # 可选
```

`InternVLAClient` 实现了它，所以：

> **`worker.py` 一个字都没改。** 旧仓的 `go2_robottrack` 客户端和新仓的 `InternVLAClient`
> 在 `worker.py` 眼里是同一个东西。

换个模型（MiniCPM、π0、别的 VLA）只需要再写一个实现 `evaluate()` 的类。
**时间轴、收发邮箱、计划过期、50 Hz 派发、底盘守卫全都不用动。**

## 5. 线程模型：慢的归慢，快的归快

| 线程 | 频率 | 干什么 | 绝对不能干什么 |
|---|---|---|---|
| ROS executor | 50 Hz | 派发计划、收画面、发状态 | ❌ 不能发 HTTP（一次 3~8 s 会卡死整个图） |
| `InferenceWorker` | 有帧就干 | `evaluate()`、HTTP 全部在这里 | ❌ 不能直接发 Twist |
| Robonix RPC | 被调用时 | 收指令、入队，`set` **等这一轮任务结束**（`get` 立即返回） | ❌ 自己不能发 HTTP（一帧 `/eval_dual` 要 3~8 s）；等是故意的，executor 对 provider 调用没有超时，plan 节点要活到任务结束 |

两个线程之间只通过两个无锁邮箱说话：

```
相机 → LatestFrameMailbox（只留最新一帧）→ 推理线程
推理线程 → PlanStore（只留最新计划 + 归零时间）→ 50 Hz 派发
```

`LatestFrameMailbox` **只保留最新一帧**：模型慢的时候，排队的旧帧没有价值，发了也是错的。

### 指令入口是非阻塞的

操作员发指令走 `VlnRuntime`（`vln_runtime.py`），三步：

1. `RUNTIME.set_instruction(text)` —— **只写一个槽位**，立刻返回；
   ROS 线程 10 Hz 的 `_pump_instruction` 把它交给客户端。

为什么绕这一下？因为 Robonix 的 RPC handler 在线程池里跑，返回慢会拖垮整个 Atlas 调用。
而真正阻塞的 `POST /eval_dual` 必须在推理线程上做。

同时它会**递增一个"代际号"**：

```
操作员: "去门口"     → generation = 5
（推理线程正在跑 generation=4 的 HTTP，可能要 8 秒）
操作员: "算了，停下"  → generation = 6
（generation=4 的响应回来了）
```

回来的那个响应**代际对不上**，于是被丢弃，返回一个零速度的 `superseded` 计划——
**不会**拿一个"没人要的任务"的答案去开底盘。

### 没人下单，就一个请求都不发

默认 `require_instruction: true`：客户端**开机是"未武装"的**。在收到操作员指令之前，
`evaluate()` 直接返回一个零速度计划**连 socket 都不开**——没有请求、没有计划、车不动。
manifest 里那句 `instruction:` 只是给 status 看的**参考文本**，不会自己开跑。

为什么要有这道闸：DualVLN 每帧都是从零重算的、还是贪心解码——**同一帧问同一个问题，
答案永远一样**。车停着、画面没变，于是"再来一帧问一次"只是在每 3~8 秒重复同一句回答。
没人下单的时候，最诚实的做法是一个字都不问。

操作员的 `stop` 也是同一件事的反面：它**既撤计划、又把任务收走**（`clear_instruction`），
所以下一帧不会又把同一个问题问一遍、照着答案再开起来。之后就一直等新指令。
（`stop` 只撤计划的话，停住的寿命只有一个推理周期——这是本次修掉的老毛病。）

## 6. 三个数：`action_step_s` 管快慢、`plan_hold_s` 管刹车、`step_settle_s` 管看路

这个设计值得单独说，因为它最容易改错。

一份回答现在是**一个动作程序**（`InferencePlan.program`）：`[1,2,1]` 就是三段，
每段一个速度加一个自己的时长，按顺序执行完再去看下一帧。

一个离散动作是**位移**（0.25 m），底盘要的是**速度**。中间要除以一个时间——**标称**时间
是 `action_step_s`（默认 1.0 s），实际时长由几何和限速共同定：

```
t  = max(action_step_s, 0.25 m / max_vx)      # 前进一步的时长
vx = 0.25 m / t                               # 位移恒等于 0.25 m
wz = radians(15) / (同样办法算出的 t)          # 转角恒等于 15°
```

计划多久没更新就该归零？这是**另一个**数，`plan_hold_s`（默认 1.5 s），纯粹是看门狗：
模型挂了、网络断了、worker 卡住，1.5 s 之后 `PlanStore` 自己把速度归零，不靠任何别的机制。
**带程序（动作块）的计划例外**：它的寿命 = `max(plan_hold_s, 程序总时长)`——模型已经把动作
块交出来了，看门狗不能在它跑一半时砍掉；如果服务器在半路死掉，最多也就是把这个有限的
动作块走完，然后归零。`RuntimeConfig` 仍然强制 `max_plan_age_s == plan_hold_s`
（同一个看门狗两个名字），但只要求 `action_step_s <= plan_hold_s`。

**为什么必须分开（2026-10-28 的教训）**：以前两者是同一个数 1.5 s，而模型实测每
**0.78 s** 就回一次。于是每份计划在新计划到来之前只被执行了约一半（15° 只转了 ~7.8°），
而且新计划一到，旧的还没走完就被换掉；反过来，模型偶尔重复一个答案时，同一个动作会被
连着推 1.5 s，**转过头**，目的地就出了画面。分开之后，一个动作走多久不再由模型的快慢决定。

### 连续驱动（`servo_enabled`，默认开）

上面几节解决的是"一步走多久、走没走够"，但循环本身还是**停—想—走**：拍图、请求、执行、
停稳、再拍图。官方客户端的循环不是这样：

- `planning_thread` 拿最新一帧 + **时间上最接近这一帧的 `/odom`**，阻塞请求模型，回来之后
  只做一件事——把目标位姿往前挪（`incremental_change_goal`）；
- `control_thread` 10 Hz 独立跑，拿最新 `/odom` 对当前目标跑 PID，**从不等待推理**；
- 两个线程之间只有一个共享的目标位姿。推理要 1 s 还是 3 s 都一样：车一直在动，
  新答案只是把目标点挪了个位置。

本栈原来把请求放进了执行循环里，所以每答一次就停一次。现在按同一个形状拆开：

1. **答案回来时读一次当前位姿**（`worker.py` 的 `pose_source` → 节点的 `_latest_pose`），
   动作就叠在这个位姿上。这正是官方那行 `homo_goal = self.homo_odom.copy()`
   （`http_internvla_client.py:313`）——**不是**叠在拍帧时的位姿上。
   这一点试过、也量过：钉在拍帧位姿上，一个来回约 1 s、车正好开掉 0.25 m = 一整步，
   目标算出来就落在车轱辘底下 → 伺服判"到了" → 刹停等下一条（实测 0.10 m/s、14 % 的时间
   停着）；而且目标朝向是 1 s 前的绝对朝向，车在这 1 s 里转过的角度会被伺服一点点纠回去，
   下一帧模型又说往另一边——**在地上看就是 S 形**。读当前位姿时这两样同时消失（0.20 m/s、
   0 % 停顿）。
2. 答案回来 → `goal_from_program(当前位姿, 动作序列)` 得到世界系目标位姿，存进 `PlanStore`。
   `1` = 沿目标朝向 +0.25 m，`2/3` = ±15°，**按顺序**累加（和官方
   `incremental_change_goal` 同一套算术），所以 `[1,2]` 是"先直行再左转"。
   轨迹类答案走 `goal_from_waypoints(当前位姿, 点串)`：同样叠在当前位姿上，取被追踪的那个点，
   朝向转到指向它——所以轨迹也有"到了"这个状态（详见上一节）。
   帧上照样带着拍帧时的位姿（`EncodedFrame.pose`）——那是"模型当时在看什么"的记录，
   现在不再参与目标计算。
3. 50 Hz 的 dispatch tick 拿最新位姿对目标跑比例控制（`servo_kp_trans=2.0`、
   `servo_kp_yaw=1.5`，即官方 `controllers.py` 的增益，它的微分项配的是 0）：平移误差
   投影到车头方向，偏航误差取最近的角；**只前进不后退**（负的钳到 0，和官方
   `if v < 0: v = 0` 一致——冲过头就停，不会倒车）。
4. 目标落进 `servo_position_tolerance_m`（5 cm）/ `servo_yaw_tolerance_deg`（3°）的盒子 →
   发 0，等下一个答案。

**闸门不再关**：worker 不再被 gate 挡住，请求永远在路上，新答案只是换目标。

**它唯一拒绝的事**：计划里写了距离/角度（离散的一步，或者一条轨迹——轨迹的"几何"就是那条
路径本身，虽然没有任何一段带着距离/角度字段），但**答话那一刻**没有新鲜位姿（odom 没开、
断了，或刚好过期）→ 停住不动（`state=servo_wait_odom`），**绝不退回"按时间盲走"**。所以 odom
半路死掉，车是停，不是瞎跑。目标为空的计划（STOP、只有 5/9、短到"就在这儿"的轨迹）本来就
是零速度，照常交给分段执行器保持。

**回退开关**：`servo_enabled: false` 回到分段执行（再配合 `lockstep_enabled: true` 就是
原来的走走停停）。两个开关互不影响：`odom_feedback_enabled` 只管"一段算不算走完"，
servo 只管"计划是不是一个目标位姿"；任一开着都会订阅 `/odom`。

离线仿真（50 Hz tick、1 s 一个答案、一节底盘惯性）：持续给 `[1,1,1,1]` 时 `vx` 平均
0.20 m/s、**没有一次归零**；持续给 `[2,2,2,2]` 时 `wz` 稳定在 17.2°/s。同一份仿真里把目标
钉在拍帧位姿上：0.10 m/s、14 % 的时间停着——这就是上车看到的"一顿一顿 + S 形"。

### 走走停停（`lockstep_enabled`，回退路径，默认关）

**下面这条路径在 `servo_enabled: true` 时不参与：**有几何的计划（离散的一步、一条轨迹）直接
走上面的伺服，`StepController` 只在 servo 关掉、或者计划本身没有目标（STOP、只有 5/9、
短于 `trajectory_stop_m` 的轨迹）时才被调用。
它仍然是完整的回退路径，也是理解老行为的地方。

`action_step_s` 只是把"走多久"钉死，还得保证**看的时候车是停着的**。做法是一个闸：

0. 只对**会动**的计划关闸：STOP（全 0）和只有 5/9 的计划速度就是 0，车没动，下一帧的
   位姿仍然是对的，照旧按模型节奏问；
1. 模型给一份会动的计划 → 立刻记版本、把整个程序装进 `StepController`，按段驱动；
2. 每一段走完自己的时长就换下一段；整条走完发 **0**，不再重推这份计划（`reason=step_done`）；
3. 再等 `step_settle_s`（默认 0.5 s，够底盘按 guard 的加速度从 0.25 m/s 停下来）；
4. 这时才**打开推理闸**：worker 丢掉闸关着期间攒的帧，等闸开之后拍的下一帧，发 `/eval_dual`；
5. 答案回来 → 回到第 1 步。

结果是每张送给模型的图，拍的时候车都停在它现在所在的位姿上，"执行的指令"和"看到的画面"
不再差一次推理的时间。代价是每个周期 = 动作块时长 + `step_settle_s` + 推理
（单步时实测约 2.3 s），车会一顿一顿地走——这是这个模式的定义，不是卡顿。

这个模式的定义就是一顿一顿地走——所以它现在默认关着，由上面的伺服取代。
想手动切回来：`servo_enabled: false` + `lockstep_enabled: true`。

### 闭环：一段什么时候才算"走完"（`odom_feedback_enabled`，默认开）

上面那套时间仍然是**开环**的：`vx = 0.25 m / t` 只是一句"如果底盘听话"的推算。实测标称
0.25 m 只走 ~0.2 m，转弯同理，而且误差不恒定（地面、坡道、上一段的滑行、guard 的加减速
斜坡都会变）。把它当成"已经走了 0.25 m"去理解下一帧，误差就一步步累积成"停在目标前面
一截""开过头还在走"。

官方客户端不是这么做的：`http_internvla_client.py` 把每个动作累加成**世界系目标位姿**
（`incremental_change_goal`：`1` = 沿目标朝向 +0.25 m，`2/3` = 目标朝向 ±15°），然后
`control_thread` 10 Hz 拿 `/odom` 跑 PID（`Kp_trans=2.0 / Kp_yaw=1.5`）追这个位姿；
轨迹走 MPC。**它从头到尾都没"按时间开环发速度"这一步。**

本栈的做法（轻量版，不改架构；servo 关掉时它是主路径，servo 开着时只用于没有几何的计划）：

1. `discrete_to_velocity` 除了速度，还把这段的几何带出来：`turn_rad = ±15°`、
   `distance_m = 0.25 m`，写进 `ProgramSegment`；
2. 节点订阅 `odom_topic`（默认 `/odom`，dry-run 也订阅），把最新位姿放进 `PoseBuffer`；
   超过 `odom_timeout_s`（0.5 s）没新样本就当"没有闭环"：分段执行退回时间，
   而 servo 会停住（见上一节）；
3. `StepController` 每 tick 拿到位姿：这一段开始时记下起点位姿，之后用
   `turn_reached` / `distance_reached` 判断**方向对、量够了没有**——转够 15°（容差
   `odom_turn_tolerance_deg` 1.5°）或走够 0.25 m（容差 `odom_distance_tolerance_m`
   0.02 m，沿起点朝向投影，所以转弯时的侧滑不算前进）就立刻换下一段；
4. 兜底 `odom_segment_timeout_factor`（2.0）：一段到 2 倍标称时长还没报"到了"就强制
   结束——被 safety gate 挡住的车、贴在墙上的轮子、乱报的 odom 都不会把一段无限挂着。
   `PlanStore` 的看门狗也随之放长到同样的上限，否则 1.5 s 的 `plan_hold_s` 会抢在
   stepper 之前把这一段砍掉（车"莫名停短了"，且 status 里只写 `stale_plan`）。

状态里 `step.feedback` 直接写当前这一段靠什么结束：`odom`（量出来的）或 `timer`（标称
时长）。**回退开关**：`odom_feedback_enabled: false` 就完全回到纯计时行为。

### 记忆窗口：`history_reset_frames`

同一条指令下每 N 个决策清一次服务端历史（原因与回退见上面"中途清历史：默认关闭"）。
**默认 0 = 永不中途清**，和官方客户端一致。写成 N 就是每 N 帧一次；原来写的 20 一次都
没触发过（本栈一个 episode 只有 5~15 个决策），后来改的 8 是每 8 秒失忆一次，2026-10-30 关掉。

**`guard_max_linear_accel_mps2` / `guard_max_angular_accel_rps2` 也跟着调了**：guard 的加速度
限制是双向的，0.30 m/s² 时单是起步斜坡就要 0.83 s，会吃掉整个 1.0 s 步长、而且
`step_settle_s` 到了车还在滑。现在 0.60 / 1.20，约 0.4 s / 0.2 s 到速度，0.5 s 内停稳。

## 7. 图片字段名：只有一个，写死

multipart 的图片字段名就是 **`image`**（文件名 `rgb`），和 `internVLN/vln_query.py` 一致。

早期版本这里有一个 `IMAGE_FIELD_CANDIDATES` 候选列表（`rgb.jpg` ↔ `image`）加运行时协商。
那是照着一段本仓自己编的接口说明写的：两份本地部署文档和官方
`http_internvla_server.py` 里字段名**从来只有一个** `image`，"协商"是在解一个不存在的问题，
而且第一次请求必然先撞一次 400。已删除，现在只有一个常量 `IMAGE_FIELD`。

`depth` 字段是可选的，本栈**从不发**：DualVLN 只吃 RGB。manifest 里原本用
`enable_depth: true` 给相机开流，深度图只在本机喂近距反射闸门
（`safety_gate.py`，`docs/SAFETY.md` §1.1），不经过网络、不进模型。
**当前部署把闸门关了，所以 `enable_depth` 也是 `false`**——深度流没有任何消费者。

## 8. 底盘守卫为什么必须存在

`RangerROSMessenger::TwistCmdCallback` 里有一段：

```
radius = |linear.x| / |angular.z|
if radius < min_turn_radius:
    SetMotionCommand(0.0, 0.0, angular.z)     # ← linear.x 被丢掉了
```

也就是说：**一个"边转边前进"的命令，在半径太小的时候会变成原地转**，机器人不再靠近目标。
一个视觉策略会在小空间里不停地发这种命令——于是它就停在那儿转圈。

chassis guard 的 `clamp_yaw` 策略把 `wz` 钳到 `|vx| / min_turn_radius`，
**保住平移分量**，让命令永远进不了上面那个分支。

另外它还补回了 Ranger 没有的保护：限速、限加速度、前向锁、0.25 s 命令超时（先于固件 500 ms 看门狗）。

## 9. 文件地图

```
packages/internvln_navigation/
  internvln_navigation/
    core.py            常量 + RuntimeConfig + PlanStore + Mailbox + 数据结构（无 ROS）
    action_adapter.py  动作码/轨迹 → 速度（纯函数，无 ROS，无网络）
    http_client.py     `POST /eval_dual` 客户端，实现 Evaluator（无 ROS）
    worker.py          推理线程（**沿用旧仓，未改逻辑**）
    vln_runtime.py     RPC 线程 ↔ ROS 线程 的指令交接（无 ROS）
    source_mux.py      navigation / vln 二选一（**沿用旧仓，只改了源名字**）
    image_preprocess.py 384 中心裁切（**沿用旧仓，未改逻辑**）
    chassis_guard.py   Ranger 底盘守卫算法（**沿用旧仓，未改逻辑**）
    ros_node.py        ROS 节点：订阅、派发、状态
    chassis_guard_node.py 守卫的 ROS 壳（沿用旧仓）
    provider.py        Robonix 生命周期 + vln/task 能力
  tests/               全部离线可跑（不需要 ROS、不需要网络）
```

「无 ROS」= 可以在这台电脑上直接跑单测。`scripts/validate_offline.sh` 就是这么做的。

## 10. 已知不确定 / 还没验的

清单摆在这，别当成已经验过：

- **`trajectory` 的点带不带 yaw**：解析器接受三元组，但只用 x/y（第三位忽略）。
  同理 `pixel_goal`（目标物在画面上的像素）我们**收下就扔**——本仓只用轨迹里的点。
- **真实单次延迟**：`plan_step_gap=4` 意味着 4 次调用里只有 1 次真跑 planner，其余可能很快。
  实测值是 p50 0.778 s / p90 0.915 s（2026-10-22 的 live 日志，493 份计划）。
- **相机型号/话题前缀**：默认 `/camera/color/image_raw`；manifest 里按 `camera_name: camera_435i`
  写的是 `/camera_435i/color/image_raw`。上线前用 `ros2 topic list` 对一遍
  （一致性检查脚本会把这两个名字锁在一起，改一个必须改另一个）。
- **`guard_min_turn_radius_m = 0.81`**：来自 Mini 3.0 手册，不是实测。上车前重标。
- **只在开发机上跑过**：单测、manifest 检查、以及对着一个"按本地文档协议写的桩服务器"
  跑通的 `/eval_dual` 客户端都已经验过（见 [ACCEPTANCE.md](ACCEPTANCE.md) 的"当前状态"）。
  但**真实模型服务器和真机一次都没跑过**，第一次上机器人时按 RUNBOOK 一步步来。

## 11. 语音旁路：`audio_client_bridge` + `speech`（不在控制链上）

语音是给 `vln/task` 多开的一个输入门，**不是控制链的一环**：

```
robonix-client 的麦克风
   → audio_client_bridge（机器人侧监听 0.0.0.0:60002，反向 WebSocket）
   → speech 服务（腾讯云 ASR：音频 → 文本）
   → liaison / pilot（对话核心；pilot 的 VLM 决定这句话是不是"下导航任务"）
   → robonix/primitive/vln/task set        ← 与手打指令同一个入口
   → 之后的链路和文本模式完全一样
```

返回方向反过来：pilot 的回复文本 → speech 的 TTS → `audio_client_bridge` → client 的音箱。

几个要点：

- **反向连接**：机器人在 60002 上监听，client 主动拨过来。机器人不需要知道 client 的地址，
  所以同一套配置在机器人换网段后照样能用。
- **没有 client 就等于没有语音**：能力还在、provider 也起得来，但没人连的时候麦克风收不到
  东西、speaker 也放不出声。底盘不受影响——这条链上没有任何东西能发速度。
- **凭据只在 `.env`**：`TENCENT_ASR_APPID` / `TENCENTCLOUD_SECRET_ID` /
  `TENCENTCLOUD_SECRET_KEY`，manifest 里只写 `${...}`。
- **构建期开关**：speech 的构建脚本读的是**进程环境变量** `SPEECH_BACKEND`（manifest 顶层
  `env:` 里那个），不是它自己的 config；所以下载/校验依赖发生在 `rbnx build`，不是运行时。
- **同一台车只能起一个占 60002 的栈**：跟随栈也用同一个原语同一个端口。两个栈本来也不能同时
  起（都占 `can_ranger`），但语音端口是第二条不能并存的理由。
- **ASR 只保证"听清"，不保证"听懂"**：把一句中文路由到 `vln/task` 的是 pilot 的 VLM。
  VLM 挂了或没配好，语音会变成"听得到、没反应"，而不是把车开走。
