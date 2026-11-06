# 命令速查 · skill-agilex-ranger-vln-rbnx

**这一页只放命令。** 每步都给了"期望看到什么"——输出不对就停在那一步，别往下走。
原理看 [ARCHITECTURE.md](ARCHITECTURE.md)，红线看 [SAFETY.md](SAFETY.md)，
逐条打勾的验收表看 [ACCEPTANCE.md](ACCEPTANCE.md)。

---

## 0. 先记住这四个名字

| 名字 | 值 |
|---|---|
| 宿主机仓库路径 | `/mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx` |
| 容器里的路径 | `/root/deploy`（= 上面那个目录） |
| 容器名 | `robonix_vln_dev`（`run_container.sh vln` 建的） |
| 镜像 | **本机没有默认 tag**，要用 `robonix:humble-orin-follow`，见 §2 |

> **本机现在实际在跑的容器叫 `robonix_internvln_dev`**（镜像是 `robonix:humble-orin-follow`），
> 是手工起的，不是 `run_container.sh` 建的。两种都能用，别搞混：
>
> ```bash
> docker ps --format '{{.Names}}' | grep -i vln     # 看现在到底有哪个
> ```

**docker 要 sudo**（本机没有 docker 组）：先 `sudo -v` 输一次密码。

---

## 1. 宿主机 · 准备（每次开机一次）

```bash
cd /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx
sudo -v
cat .env
```

- [ ] `.env` 里有 `INTERNVLA_SERVER_URL`，且是**根地址**：`http://10.0.105.11:5801`
      ——**不带** `/eval_dual`（客户端自己会加）

改完 `.env` 不用重装什么，`start.sh` 每次读。

### 1.1 一个 CAN 只能有一个主人（**上车前必做**）

```bash
pgrep -af 'ranger_base_node|velocity_guard'      # 应该没有输出
```

有输出 = 导航栈还活着 → 先停它，再起本栈。两套同时跑 = 两套 PID 抢一个 CAN。

---

## 2. 宿主机 · 起容器

```bash
ROBONIX_VLN_IMAGE=robonix:humble-orin-follow \
    bash scripts/run_container.sh vln
```

期望输出（看这三行）：

```
  USB major       = 189  (cgroup rule: c 189:* rmw)
  Orbbec camera   = 2bc5:xxxx
  USB-CAN adapter = 1d50:xxxx
  -> robonix_vln_dev started, DeviceCgroupRules=[c 189:* rmw]
```

- 不写 `ROBONIX_VLN_IMAGE=...` 会 `[fatal] image 'robonix:humble-orin-deps-20260821'
  is not on this robot` —— 脚本默认那个 tag **本机没有**，这是已知的，用上面那行覆盖。
- `[warn] Orbbec/USB-CAN 未检测到` 只是警告，容器照起——但上车一定没画面/没 CAN，先查 USB。
- 这脚本每次都 **stop → rm → run**（故意重建，顺手清僵尸进程）。**容器里没存东西**，
  代码和构建产物都在挂载的宿主目录里，重建不丢。

## 3. 进容器

```bash
docker exec -it robonix_vln_dev bash        # 或 robonix_internvln_dev
cd /root/deploy
```

---

## 4. 容器内 · 构建

```bash
bash build.sh
```

构建完**必须**确认代码生成有产物（没有它 provider 起不来）：

```bash
ls rbnx-build/codegen/robonix_mcp_types/ | grep internvln
# 期望：internvln_navigation_control_mcp.py
```

改过 `capabilities/*.srv` 或 manifest 之后，**必须重新 build**。

语音的两个包不随 `build.sh` 构建（一个是 `rbnx-boot/cache/` 里 clone 下来的原语，一个在
robonix 源码树里）。第一次用、或者清过缓存之后，手动各 build 一次：

```bash
rbnx build -p rbnx-boot/cache/primitive-audio-client-bridge-rbnx --no-update-check
SPEECH_BACKEND=tencent rbnx build -p /root/workspace/services/speech --no-update-check
# 期望：✓ Package 'robonix.primitive.audio.client_bridge' build finished
#       ✓ Package 'com.robonix.example.speech_service' build finished
#       （speech 那行前面还有 "Tencent ASR/TTS imports OK; no local model or GPU required"）
```

`SPEECH_BACKEND` 必须显式给：构建脚本读的是**进程环境变量**，不是包自己的 config。

---

## 5. 离线自检（宿主机/容器都行，不碰机器人、不碰相机、不碰模型）

```bash
bash scripts/validate_offline.sh
```

期望最后两行：

```
manifest consistency OK
offline validation OK
```

只想要 manifest 那部分（快）：

```bash
python3 scripts/validate_manifest_consistency.py
```

切 live 之后跑它，输出里会出现 `(ARMED)` —— 那是给你提神的。

---

## 6. 证明模型能连上（**别跳，这一步最省事**）

```bash
python3 scripts/probe_internvla_server.py \
    --url "$(grep -E '^INTERNVLA_SERVER_URL=' .env | cut -d= -f2-)" \
    --image /tmp/frame.jpg
```

期望结尾：`PROBE OK: the server speaks the protocol this client expects`
（`--image` 不给也行，但合成图会让模型说胡话。）

**它不碰 ROS、不碰 CAN、不碰轮子**——车 armed 着跑也没关系。

| 看到 | 意思 |
|---|---|
| `Connection refused` / `timed out` | 网络/防火墙/端口，先解决这个 |
| HTTP **404** | 你连上的不是 DualVLN 服务 |
| HTTP **405** + `Allow: POST` | 正常，这就是"路由在"的判据（用来探活的那条 curl 也靠它） |
| 报 `missing file field` / 400 | 服务器要的图片字段名不是 `image` → **版本对不上**，不是换个字段名能糊过去的 |

---

## 7. dry-run · 把链路跑通（机器人不会动）

### 7.1 起栈

```bash
bash start.sh
```

它会：读 `.env` → 探一次 `/eval_dual`（GET 期望 **405**）→ 配 CAN → 起 zenoh router → `rbnx boot`。

期望多一行：

```
DualVLN server http://10.0.105.11:5801: POST /eval_dual is registered (GET -> 405, Allow: POST, OPTIONS)
```

`mode: dry-run` 时探不到只会警告一句、继续起；`mode: live` 时**直接拒绝启动**（故意的）。

### 7.2 看链路（**另开一个终端**）

```bash
docker exec -it robonix_vln_dev bash
source /opt/ros/humble/setup.bash && export RMW_IMPLEMENTATION=rmw_zenoh_cpp
cd /root/deploy && python3 scripts/watch_vln_chain.py 2>/dev/null
```

dry-run 下**本来就是这样**，三行 `pub 0` 不是故障：

```
VLN command chain   14:22:07   mode=dry-run   (Ctrl-C 退出)
/ranger/vln/cmd_vel_raw        0   1     0   -  -  dry-run: no velocity publisher by design
/cmd_vel_nav                   0   1     0   -  -  dry-run: no velocity publisher by design
/cmd_vel                       0   2     0   -  -  dry-run: no velocity publisher by design

/ranger/vln/status  (node status, 12 msgs)
    age  0.4s  mode=dry-run  state=running  episode=1  frames=180  flushes=8  failures=0  healthy=True
    lockstep=False  step=servo  segment=0/0  goal=-  feedback=timer  looking=yes  (nominal 1.0s + 0.5s settle)
    odometry: fresh
    servo: chasing (1.24, 0.08, 28.0deg)  err=0.214m dyaw=-2.5deg
    last_note: discrete_action[2] -> 1 step(s) over 1.00 s: turn left 15.0 deg over 1.00 s
```

**dry-run 只有这几个判据**（表头写着 `mode=dry-run` 就别去看 `pub`）：

- [ ] `age` 一直很小、`msgs` 在涨 → 节点活着
- [ ] `frames` 持续在涨、`failures=0 healthy=True` → 模型每帧都答上了
- [ ] `/cmd_vel` 的 `pub` **= 0**（硬要求：dry-run 谁都不能发速度）
- [ ] `start.sh` 终端里 `dry-run prediction: vx=… wz=…` 随画面变化（每帧的换算结果在这看）

`pub=1`、`Δmsg` 在涨这些判据是 **live 之后**才成立的。

### 7.3 发指令 / 停

**链路是"你下单它才动"**：默认 `require_instruction: true`，指令没来之前节点一次模型都不问、
也不发速度（status 里 `armed=False`、`frames=0`、`last_note: awaiting an operator instruction`）。
发指令是开机后的第一件正经事：

```bash
scripts/set_vln_instruction.sh "go to the chair and stop"
scripts/set_vln_instruction.sh stop
```

**立刻返回**（只入队）——真正阻塞的那一帧在推理线程上做。
发完回 watcher 看：`armed` 变 `True`、`episode=` 加一、`frames` 从 0 开始涨。
指令写成**有终点的任务**（"走到椅子前停下"），别写"一直往前走"这类口号——DualVLN 是到达型
模型，没有目标物的句子会被它判成"已经做完了"而一直回 `STOP`。

`stop` 不只是撤计划：它同时**把任务收走**，之后不再请求模型、车停住等新指令。

走 Robonix 能力也行：

```bash
rbnx ask -c robonix/primitive/vln/task '{"operation":"set","instruction":"go to the chair and stop"}'
rbnx ask -c robonix/primitive/vln/task '{"operation":"get"}'
rbnx ask -c robonix/primitive/vln/task '{"operation":"stop"}'
```

`set` 会**阻塞到这一轮任务结束**（`status: finished` / `stopped`；超时 `set_block_timeout_s` 则回 `running`）。
等的时候终端像卡住，其实是任务在跑——一个任务一个 plan 节点，Pilot 不会中途重派。想看中间状态用 `get`。
`get` 在没发指令或 `stop` 之后回 `status: awaiting-instruction` / `armed=False`。
模型**一直卡在 STOP**（0 = 判定"到了"）时——连续 ≥ 20 帧且持续 ≥ 30 s
（`finish_after_stops` / `finish_after_stops_min_s`）——`get` 回
`status: finished` / `running: false`，`instruction` 是刚结束的那条：计划已撤、
不再请求模型，executor 可以据此收尾；发新指令即重新开始。偶尔的 0 不会结束任务。
前向深度闸门关闭时，`get` 回 `status: blocked` / `running: false`（原语已被停掉、
不再请求模型）；这时 `set` 回 `status: held`，指令记着，闸门放行后自动开始。
**当前部署闸门是关掉的**（`safety_gate_enabled: false`，深度也在相机侧关流），所以不会出现
`blocked`；这几行只在重新打开闸门后才用得上。

### 7.4 语音（可选，不影响上面任何一步）

语音包在 manifest 里，但**要有 client 连上来才有声**。机器人侧先确认在听：

```bash
ss -lntp | grep 60002
# 期望：LISTEN ... 0.0.0.0:60002 ... （audio_client_bridge）
```

然后在一台装了 robonix-client 的电脑上连到 `<机器人IP>:60002`，说一句
"走到椅子前停下"。看两处：

```bash
# 容器里看 ASR / TTS 日志
docker logs -f robonix_internvln_dev 2>&1 | grep -i -E "speech|asr|tts|audio"
# 另一个终端看任务有没有真的落到 vln/task
rbnx ask -c robonix/primitive/vln/task '{"operation":"get"}'
```

- ASR 出文本、但 `vln/task` 没变化 → 问题在 pilot 的 VLM（它负责把这句话路由成导航任务），
  不是 ASR；
- 连 60002 都连不上 → 先查 `ss`、再看容器是不是 `--network host`；
- 没 client 时麦克风/音箱是哑的，这是**正常**的，底盘不受影响。

> 同一台车不能同时起跟随栈和本栈：两边都占 `can_ranger`，也都要监听 60002。
>
> 语音只在**重启栈**后才生效——manifest 是新加的，正在跑的进程不会自己长出 provider。

---

## 8. 切 live（**会动，手不离遥控器**）

### 8.1 改哪一行

只有一行，在 `robonix_manifest.yaml` 的 `internvln_navigation` 段里（第 141 行）。
**别手改，用脚本**——它会顺带把不该动的地方挡住：

```bash
scripts/set_vln_mode.sh                  # 看现在是哪种模式
scripts/set_vln_mode.sh status           # 同上
scripts/set_vln_mode.sh live --yes       # 切 live（必须带 --yes）
scripts/set_vln_mode.sh dry-run          # 切回来（安全方向，不用确认）
```

它只做**一处**编辑，原子写回，缩进/行尾注释/文件权限都保留。四种拒绝：

| 退出码 | 什么时候 |
|---|---|
| `3` | 切 live 没带 `--yes` |
| `4` | **导航栈还在跑**（一个 CAN 只能有一个主人）→ 先停它 |
| `1` | manifest 里 `mode:` 不止一处（脚本不猜哪个是开关） |
| `2` | 参数写错 |

> 包内那份 `packages/internvln_navigation/config/internvln_navigation.yaml` 里也有个
> `mode:`，**脚本故意不碰它**：部署这条路（`start.sh` → `rbnx boot` → `provider.initialize`）
> 用的是 manifest 里的 config；包内那份只在用 launch 文件单独跑包时才生效，
> 留在 `dry-run` 正是那条路该有的安全默认值。
> `start.sh` 判断模式时也只 grep manifest 这一份。

万一脚本用不了，手改也行——**只改这一行**：

```bash
grep -n 'mode:' robonix_manifest.yaml        # 确认只有一处是你以为的那处
sed -i 's/^      mode: dry-run$/      mode: live/' robonix_manifest.yaml
grep -n 'mode: live' robonix_manifest.yaml   # 改完再看一眼
```

回滚就把 `live` 换回 `dry-run`（同一条 sed 反着写）。

### 8.2 切 live 的四步

```bash
# 1) 先停
bash stop.sh
# 2) 改那一行
scripts/set_vln_mode.sh live --yes
# 3) 自检（应出现 (ARMED)）
python3 scripts/validate_manifest_consistency.py
# 4) 确认了导航栈停了、前方 10 m 净空、遥控器在手，再起
bash start.sh
```

`start.sh` 这时会打印：

```
WARNING: mode is live -- the chassis guard will be started and this
stack WILL drive the wheels. Keep a physical remote in hand.
```

**看到这行说明 guard 要起来了。没看到 = 你其实还在 dry-run。**

---

## 9. 实机验证（live，**按这个顺序，断在哪段就往回查**）

每一段都留证据（截图 / 日志）。前一段不过，**不要**进下一段。

### 第 1 段 · 栈起来了没

- [ ] `start.sh` 打印了上面那句 `WARNING: mode is live ...`
- [ ] watcher 表头写 `mode=live`
- [ ] `/ranger/vln/status` 每秒一条、`age` 小、`frames` 在涨

### 第 2 段 · 三个发布者对不对（**这一步只看话题，不发指令**）

- [ ] `/cmd_vel` 的 `pub` **恰好为 1**（只有 chassis guard）
- [ ] `/cmd_vel_nav` 的 `pub` **恰好为 1**（只有 source mux）
- [ ] `/ranger/vln/cmd_vel_raw` 的 `Δmsg` 持续 > 0

`/cmd_vel` 的 `pub` 是 0 → guard 没起来，看 `rbnx` 日志，别发指令。
`pub` ≥ 2 → **有人和 guard 抢轮子，立刻停**。

### 第 3 段 · 直走一条（车第一次动）

```bash
scripts/set_vln_instruction.sh "go to the chair and stop"
```

- [ ] `/cmd_vel` 上 `vx > 0`、`wz ≈ 0`、**`vx` 从不为负**
- [ ] `|vx| ≤ 0.25`、`|wz| ≤ 0.30`（超了 = guard 没生效）
- [ ] 全程 `linear.y == 0`（底盘没切 4WS 转向模式）
- [ ] **车确实往前走了**，没有"原地打转不前进"

### 第 4 段 · 转一条

```bash
scripts/set_vln_instruction.sh "turn left"
```

看 `last_note` 分清模型这次回的是哪种，**两种都算通过**：

- [ ] 回**动作码** 2/3 → `vx == 0`、`wz > 0`（纯原地旋转），guard 日志里能看到 `clamp_yaw`
- [ ] 回**轨迹**且方位角 ≤ 10.7° → `wz > 0` 且 `vx > 0`：车走一段左弧（`last_note` 里有 `arc radius`）；
      方位角 > 10.7° → `vx == 0`、`last_note` 里写 `pivoting`（底盘的最小转弯半径转不过去）
      只有轨迹弯得比底盘最小半径（0.81 m）还紧时，才变成 `vx == 0` 的原地转，
      note 里会写 `pivoting`

### 第 5 段 · 主动构造失败（每条都要试一次，**这是最值钱的一段**）

| 怎么构造 | 期望 |
|---|---|
| 停掉算力机上的模型服务（或拔网线） | `failures` 涨到 3 → 计划被撤销 → **车停** |
| 拔相机 | `frames` 停止增长 → 计划到期 → **车停** |
| 发一条指令后立刻 `stop` | 旧结果被丢弃，不执行 |
| `Ctrl-C` 掉 `start.sh` | 先发零速度再退出 → **车立刻停** |
| `bash stop.sh` | 无进程残留（**脚本不打印 WARNING**） |

`stop` 按完，速度在**一个 `plan_hold_s`（1.5 s）内**归零。

### 第 6 段 · 长稳

- [ ] ≥ 10 min dry-run 无 OOM、无降频
- [ ] ≥ 5 min live 运动窗口内三路命令连续、无中断
- [ ] `tegrastats` 看 RAM/SWAP/温度（本机不做 CUDA，看 CPU/内存就够）
- [ ] 停完之后 `/cmd_vel` 回到 0，且**退出后不再有发布者**

---

## 10. 收工 / 回滚（走人前做）

```bash
bash stop.sh                                          # 容器里
scripts/set_vln_mode.sh dry-run                       # 宿主机/容器都行，不用 --yes
python3 scripts/validate_manifest_consistency.py      # 确认不再提示 (ARMED)
```

- [ ] `can_ranger` 空闲（`pgrep -af 'internvln_navigation\.'` 无输出）
- [ ] 然后导航栈才能起来

```bash
docker stop robonix_vln_dev      # 容器停下；挂载目录和构建产物都还在
```

模型服务器在远端，本仓不管它，停不停随你。

---

## 11. 三层急停

| 层 | 命令 | 效果 |
|---|---|---|
| 软停 | `scripts/set_vln_instruction.sh stop` | 撤销计划，1.5 s 内归零 |
| 硬停 | `Ctrl-C` 掉 `start.sh` | 节点 close 前先发零速度 |
| 物理 | 遥控器 / 拍电门 | **永远保留这一层** |

---

## 12. 排障速查

| 现象 | 先看 | 常见原因 |
|---|---|---|
| 起来了但 `frames=0` | `ros2 topic list \| grep image` | 相机话题不对；manifest 的 `rgb_topic` 和实际话题必须**一字不差** |
| `healthy=False`，`last_error` 有字 | 状态行的 `last_error` | 模型服务挂了 / 网址写错 / 防火墙 |
| 状态行完全不动 | `/ranger/vln/status` 的 `age` | 节点没起来，看 `logs/` |
| `episode=` 有值但 `frames` 不动 | 相机断流 | USB 掉线，`lsusb` 看相机在不在 |
| dry-run 下三行 `pub 0` | 表头 `mode=` | **正常的**，别修 |
| live 下 `/cmd_vel` 的 `pub=0` | `rbnx` 日志 | guard 没起来 |
| live 下 `/cmd_vel` 的 `pub≥2` | 谁在发 | **立刻停**，有人和 guard 抢 |
| 车原地打转不前进 | `/cmd_vel` 那行 `vx=0 wz≠0` | 三种都正常，看 `last_note` 分清：动作码 2/3（纯原地转）、轨迹被判"比底盘最小半径紧"（note 写 `pivoting`）、或 `clamp_yaw` 钳制 |
| 一直直走不拐弯 | `last_note` 里的 `arc radius` | 模型画的线近处本来就几乎不偏，`wz` 小是正常的；缩放方位角的增益已经整段回退，不要再加回来（会甩） |
| 完全不动但 `/cmd_vel` 有非零值 | `ip -details link show can_ranger` | CAN 没起 / 底盘没上电 |
| 指令发了没反应 | 状态行的 `pending=` | RPC 的 `set` 会阻塞到任务结束（默认 900 s），期间用 `get` 看推理线程有没有在跑 |
| 切 live 被拒（退出码 4） | `pgrep -af 'ranger_base_node\|velocity_guard'` | 导航栈还在跑，一个 CAN 不能有两个主人 |
| 切 live 被拒（退出码 3） | 你忘了 `--yes` | 那是故意的：武装要有一个明确动作 |
| 切 live 被拒（退出码 1） | `grep -n 'mode:' robonix_manifest.yaml` | manifest 里 `mode:` 不止一处，脚本不猜 |

抓现场（三份一起给）：

```bash
docker exec robonix_vln_dev bash -lc 'cd /root/deploy && tail -200 logs/*.log 2>/dev/null'
docker exec robonix_vln_dev bash -lc 'ros2 topic echo --once /ranger/vln/status std_msgs/msg/String'
docker exec robonix_vln_dev bash -lc 'ros2 topic list'
```

---

## 13. 禁忌

1. **不要**在 `mode: live` 时无人看管、无遥控器地跑。
2. **不要**让本栈和导航栈同时跑（一个 CAN 只能有一个主人）。
3. **不要**绕过 chassis guard 直接连 `ranger_chassis`。
4. **不要**给模型服务器开 `0.0.0.0/0` 或映射到公网——**它没有任何鉴权**。
5. **不要**把 `INTERNVLA_SERVER_URL` 写成带 `/eval_dual` 的完整地址（`start.sh` 会拦住你）。
6. **不要**在 dry-run 下因为看到 `pub 0` 就去"修"它——那就是设计。
