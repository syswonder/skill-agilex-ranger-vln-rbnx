# skill-agilex-ranger-vln-rbnx · 命令手册

Ranger Mini v3 上的 **InternVLA-N1（DualVLN）视觉导航**整套操作命令。想先看"它是怎么设计的"，
见 [docs/OVERVIEW.md](docs/OVERVIEW.md)（原 README）和 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

> **本仓不是 git 仓**。回退靠 [docs/CHANGELOG.md](docs/CHANGELOG.md) 里每次改动的"能回退吗"一段。

---

## 0. 三条红线（动之前先读）

1. **默认 dry-run**：`mode: dry-run` 时节点**不建速度发布者**，chassis guard 根本不起，车不会动。
   要动必须 `scripts/set_vln_mode.sh live --yes` **并重启栈**。
2. **没有避障、没有定位、没有软件急停**。唯一的本地保护是 chassis guard 的限速/限加速度/前向锁/
   命令超时；深度近距反射闸门目前是**关的**（`safety_gate_enabled: false`），关着时连深度都不开流。
3. **一个 CAN 只能有一个主人**：本栈和导航栈都占 `can_ranger` / `/cmd_vel`，**绝不同时跑**。
   模型服务器**没有鉴权**，防火墙只放行机器人网段，不许 `0.0.0.0/0`。

---

## 1. 约定

| 名字 | 值 |
|---|---|
| 宿主机仓库路径 | 本 README 所在目录（下面写 `<repo>`）。`scripts/run_container.sh` 内写死为 `/mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx` |
| 容器内路径 | `/root/deploy`（= 仓库 bind mount） |
| Robonix 工作区（宿主机 → 容器） | `/mnt/ssd/wxf/jiaofu/robonix` → `/root/workspace` |
| 实际在用的容器名 | **`robonix_internvln_dev`**（手工起的那个） |
| `run_container.sh vln` 生成的容器名 | `robonix_vln_dev`（脚本默认；两个名字都存在过，**先 `docker ps` 确认在用哪个**） |
| ROS 环境 | `source /opt/ros/humble/setup.bash && export RMW_IMPLEMENTATION=rmw_zenoh_cpp` |
| docker | **需要 sudo**（没有 docker 组）。先 `sudo -v` 缓存一次密码 |

所有容器内命令都长这样：

```bash
sudo docker exec -it robonix_internvln_dev bash -lc 'cd /root/deploy && <命令>'
# 需要 ROS 的命令：
sudo docker exec -it robonix_internvln_dev bash -lc \
  'source /opt/ros/humble/setup.bash; export RMW_IMPLEMENTATION=rmw_zenoh_cpp; cd /root/deploy && <命令>'
```

---

## 2. 入口脚本总表

| 脚本 | 在哪跑 | 用法 | 干什么 |
|---|---|---|---|
| `start.sh` | 容器内，`/root/deploy` | `bash start.sh` | 读 `.env` → 查 `INTERNVLA_SERVER_URL` → 探服务器 → 配 `can_ranger` → 必要时起 `rmw_zenohd` → `rbnx boot --no-update-check -f robonix_manifest.yaml` |
| `stop.sh` | 容器内 | `bash stop.sh` | `rbnx shutdown`；**没有 `rbnx-boot/state.json` 也能停**（去扫 `rbnx boot` 父进程，TERM→KILL） |
| `build.sh` | 容器内 | `bash build.sh` | `rbnx build -f robonix_manifest.yaml`（幂等，可反复跑） |
| `setup-hw.sh` | **只在宿主机** | `./setup-hw.sh`（会自己 sudo） | 雷达网口/IP、`can_ranger`、底盘 CAN 模式、Orbbec、NVIDIA CDI |
| `scripts/run_container.sh` | 宿主机 | `bash scripts/run_container.sh vln` | **停→删→重建**容器（`--init --network host` + USB 设备 cgroup 规则） |
| `scripts/set_vln_mode.sh` | 宿主机或容器 | 见 §5 | 只改 manifest 里**那一行** `mode:`，从不启停栈 |
| `scripts/set_vln_instruction.sh` | 容器内（要 ROS 环境） | 见 §4.3 | 往 `/ranger/vln/instruction` 发一句话，或 `stop` |
| `scripts/validate_offline.sh` | 哪都行，不碰硬件 | `bash scripts/validate_offline.sh` | 包内单测 + manifest 一致性校验 |
| `scripts/validate_manifest_consistency.py` | 哪都行 | `python3 scripts/validate_manifest_consistency.py` | 静态九项检查，末尾打印 `manifest consistency OK` |
| `scripts/watch_vln_chain.py` | 容器内（要 ROS 环境） | `python3 scripts/watch_vln_chain.py 2>/dev/null` | 一条链四个话题的实时表格 |
| `scripts/probe_internvla_server.py` | 哪都行，不碰 ROS/CAN | 见 §6 | 用真 client 打真服务器的 `POST /eval_dual` |
| `packages/internvln_navigation/tests/run_offline_tests.sh` | 哪都行 | `bash tests/run_offline_tests.sh` | 包内单测（`unittest discover`） |

`packages/internvln_navigation/start.sh` / `build.sh` 是 **rbnx 的包入口**，由 `package_manifest.yaml`
调用，**不要手工执行**。

---

## 3. 起服务 / 停服务

### 3.1 起（容器内）

```bash
# 宿主机
sudo docker ps --format '{{.Names}}'         # 确认在用的容器名
sudo docker exec -it robonix_internvln_dev bash

# 容器内
cd /root/deploy
bash build.sh            # 改了代码才需要；幂等
bash start.sh            # 起栈（mode 是什么就是什么）
```

`start.sh` 自己会做这几件事，失败时按它打印的原因处理：

- `.env` 不存在 → 不会静默用默认值，先 `cp .env.example .env` 填 `INTERNVLA_SERVER_URL`；
- URL 带路径（`/eval_dual`、`/health`、`/task/start`、`/task/report`）→ 直接拒绝，写**BASE URL**；
- 服务器不通：**live 拒绝起栈**，dry-run 只警告；
- `can_ranger` 没 up → 自己 `ip link` 配 500000；
- 7447 没人监听 → 起 `rmw_zenohd`，日志在 `logs/rmw_zenohd.log`，pid 在 `.run/rmw_zenohd.pid`。

**起之前必须确认导航栈不在跑**：

```bash
pgrep -af 'ranger_base_node|velocity_guard'      # 必须为空
rbnx shutdown -f <导航仓>/robonix_manifest.no-arm.yaml   # 若在跑，先停它
```

### 3.2 停（容器内）

```bash
bash stop.sh
```

三种情况行为不同：

| 情况 | `stop.sh` 做什么 |
|---|---|
| 有 `rbnx-boot/state.json` | `rbnx shutdown -f robonix_manifest.yaml` |
| 没 state 文件但 `rbnx boot` 还活着（典型：上一次 boot 报 `atlas 50051 已被占用`，state 被删了） | 找 boot 父进程 SIGTERM，等 10 s，不退再 SIGKILL |
| 都没有 | 打印 `no Robonix boot state; nothing to stop` |

停完它会检查残留并**只警告不杀**：

```bash
pgrep -af 'internvln_navigation\.(provider|ros_node|chassis_guard_node)'
# 有残留就手工：pkill -f 'internvln_navigation\.'
```

其它停法：

```bash
# 软停：只撤掉当前任务，栈还活着
scripts/set_vln_instruction.sh stop

# 硬停：Ctrl-C 掉 start.sh（节点关之前会发零速）
sudo docker stop robonix_vln_dev        # 停生成的容器；build 产物在挂载盘里不会丢
```

### 3.3 开机 / 换容器

```bash
# 宿主机：恢复硬件（会短暂 down/up 一次 can_ranger）
./setup-hw.sh

# 重建容器（镜像 tag 必须是本机已有的，镜像不在 registry 上）
ROBONIX_VLN_IMAGE=robonix:humble-orin-follow bash scripts/run_container.sh vln
bash scripts/run_container.sh vln            # 用脚本默认 tag（robonix:humble-orin-deps-20260821）
```

镜像 tag 不对时脚本会列出现有 `humble-orin` 镜像并退出——把真实 tag 传进 `ROBONIX_VLN_IMAGE` 即可；
离线 tar 则 `docker load -i <tar>`。

---

## 4. dry-run 全流程（机器人不会动）

### 4.1 离线自检（不碰硬件、不碰模型）

```bash
bash scripts/validate_offline.sh        # 期望最后一行 offline validation OK
python3 scripts/validate_manifest_consistency.py   # 期望 manifest consistency OK
```

### 4.2 证明模型能连上（最省事的一步，别跳）

```bash
# 用仓库自带探针（走的是节点同一份 client）
python3 scripts/probe_internvla_server.py --url "$INTERNVLA_SERVER_URL"
python3 scripts/probe_internvla_server.py --url "$INTERNVLA_SERVER_URL" --image /tmp/frame.jpg

# 参数：--url(必填) --instruction --image --size --quality --frames --timeout
# 期望：PROBE OK: the server speaks the protocol this client expects
#       episode / requests / last response body
```

裸 curl 验协议（GET 期望 405，说明路由在）：

```bash
curl -sS --noproxy '*' --max-time 5 -o /dev/null -w 'HTTP %{http_code}\n' "$INTERNVLA_SERVER_URL/eval_dual"
# 真打一帧（multipart）：
curl -sS --noproxy '*' --max-time 30 -w '\nHTTP %{http_code}\n' -X POST \
  -F 'json={"reset":true,"instruction":"Go to the chair and stop."}' \
  -F "image=@/tmp/frame.jpg;type=image/jpeg" "$INTERNVLA_SERVER_URL/eval_dual"
```

### 4.3 起栈 + 发指令

```bash
# 1) 确认是 dry-run（脚本只改文件，不启停）
scripts/set_vln_mode.sh                  # 不带参数 = 看当前 mode 和跑着什么
scripts/set_vln_mode.sh dry-run          # 安全方向，不需要确认

# 2) 起栈（必须重启才吃 mode，见 §5）
bash start.sh

# 3) 另一个终端看链路
python3 scripts/watch_vln_chain.py 2>/dev/null

# 4) 下一条指令（只入队，立刻返回；模型慢/不可达也一样）
scripts/set_vln_instruction.sh "go to the chair and stop"
scripts/set_vln_instruction.sh stop

# 5) 能力接口（同一条路；注意 rbnx 的 set 会一直等到任务结束才返回）
rbnx ask -c robonix/primitive/vln/task '{"operation":"set","instruction":"go to the chair and stop"}'
rbnx ask -c robonix/primitive/vln/task '{"operation":"get"}'
rbnx ask -c robonix/primitive/vln/task '{"operation":"stop"}'
# 包内 README 记的另一种写法：rbnx ask <provider> robonix/primitive/vln/task '{...}'
```

dry-run 下每帧动作会打成一行：

```
dry-run prediction: vx=…, wz=…, state=…, age=…, gate=clear|blocked (…, nearest=…), episode=…
```

---

## 5. 切 live（会动，手不离遥控器）

```bash
scripts/set_vln_mode.sh                        # 看现状
scripts/set_vln_mode.sh live --yes             # 改 manifest 那一行（没有 --yes 会被拒，退出码 3）
bash stop.sh && bash start.sh                  # 必须重启才生效
```

`set_vln_mode.sh` 的退出码：

| 码 | 含义 |
|---|---|
| 0 | 改好了 / 本来就是 |
| 1 | manifest 缺失、不可读、`mode:` 行不唯一 |
| 2 | 用法错 |
| 3 | `live` 没带 `--yes` |
| 4 | 导航栈还占着 `can_ranger`（`ranger_base_node` / `velocity_guard` 在跑） |

**不建议手改**，真要走手工兜底（只改这一行）：

```bash
grep -n 'mode:' robonix_manifest.yaml
sed -i 's/^      mode: dry-run$/      mode: live/' robonix_manifest.yaml
grep -n 'mode: live' robonix_manifest.yaml
```

live 验收顺序（断在哪段就往回查）：

1. 栈起来了没 → `bash start.sh` 打印 + `ros2 topic list`
2. 三个速度话题对不对 → `python3 scripts/watch_vln_chain.py`
3. 直走一条（`set_vln_instruction.sh "…"`）
4. 转一条
5. **主动构造失败**：拔网线 / 杀服务器 / `stop`，确认车自己停
6. 长稳：`tegrastats` + 30 min 连续跑

---

## 6. 看日志

### 6.1 日志在哪

| 路径 | 内容 |
|---|---|
| `<repo>/logs/` | 栈级日志（`rbnx.log`、`rmw_zenohd.log`、自己重定向的 `start-*.log`；录制 `vln-capture/`） |
| `<repo>/rbnx-boot/logs/` | **每个组件一份**：`atlas/executor/pilot/soma/vitals/liaison`、`front_camera`、`ranger_chassis`、`robot_description`、`internvln_navigation`、`audio_client_bridge`、`speech`、`bootstrap` |
| `<repo>/packages/internvln_navigation/logs/codegen.log` | 代码生成 |
| `<repo>/packages/internvln_navigation/.build/` | colcon build/install |
| `<repo>/rbnx-boot/state.json` | `stop.sh` 唯一的入口；**没有它不代表没在跑** |

### 6.2 看日志的命令

```bash
# 栈级
sudo docker exec robonix_internvln_dev bash -lc 'cd /root/deploy && tail -200 logs/*.log 2>/dev/null'
tail -f logs/rbnx.log
tail -80 logs/rmw_zenohd.log

# 组件级（最常用：节点自己的日志在这里）
tail -f rbnx-boot/logs/internvln_navigation.log
tail -f rbnx-boot/logs/ranger_chassis.log
tail -f rbnx-boot/logs/atlas.log

# 容器 stdout（语音/audio 这类）
sudo docker logs -f robonix_internvln_dev 2>&1 | grep -i -E 'speech|asr|tts|audio'

# 关键字符串
grep -n 'dry-run prediction' rbnx-boot/logs/internvln_navigation.log | tail
grep -n 'discrete_action'    rbnx-boot/logs/internvln_navigation.log | tail
```

---

## 7. 看话题与状态

### 7.1 一把梭

```bash
source /opt/ros/humble/setup.bash; export RMW_IMPLEMENTATION=rmw_zenoh_cpp
cd /root/deploy && python3 scripts/watch_vln_chain.py 2>/dev/null
```

订阅 `/ranger/vln/cmd_vel_raw`、`/cmd_vel_nav`、`/cmd_vel`、`/ranger/vln/status`，每秒刷新一张表。

### 7.2 单条命令

```bash
ros2 topic list
ros2 topic list | grep image
ros2 topic hz /odom
ros2 topic echo --once /ranger/vln/status std_msgs/msg/String
ros2 topic info /cmd_vel                      # Publisher count：0=dry-run，1=live，>1=危险
ros2 topic pub --once /ranger/vln/instruction std_msgs/String "{data: 'walk to the door'}"
rbnx list                                     # provider 列表（speech / audio_client_bridge …）
```

### 7.3 话题表

| 话题 | 类型 | 谁写 → 谁读 | 含义 |
|---|---|---|---|
| `/camera_435i/color/image_raw` | Image | 相机 → 节点 | 模型输入，必须等于 manifest `rgb_topic` |
| `/camera_435i/depth/image_raw` | Image | 相机 → 闸门 | 只给深度闸门用，**不发给模型**；当前 `enable_depth: false` |
| `/ranger/vln/instruction` | String | 操作员 → 节点 | 指令入口 |
| `/ranger/vln/status` | String(JSON) | 节点 → 观察者，1 Hz | 状态（字段见 §7.4） |
| `/ranger/vln/cmd_vel_raw` | Twist | 节点 → 源选择器 | 模型换算出的原始速度 |
| `/ranger/vln/nav_cmd_vel_raw` | Twist | 导航 → 源选择器 | 本 profile 不用 |
| `/cmd_vel_nav` | Twist | 源选择器 → guard | 选择器的唯一输出 |
| `/cmd_vel` | Twist | chassis guard → `ranger_chassis` | **只有 guard 能写** |
| `/odom` | Odometry | 底盘 → 节点 | 闭环反馈 + 伺服位姿 |

端口：50051 atlas / 50061 executor / 50091 soma / 50093 vitals / 50081 liaison / 50071 pilot /
60002 audio 反向 WebSocket / 5801 模型服务器 / 7447 zenoh 路由。

### 7.4 `status` JSON 字段

| 字段 | 含义 |
|---|---|
| `mode` / `source` | 当前模式；选中的速度源 |
| `plan.reason` | 计划状态（`active_plan` / `stale_plan` / `expired` …） |
| `command.vx/wz/reason/blocked/published` | **过完安全闸门后真正要发出去的速度**；`published=false` 表示 dry-run（只是"本来会发什么"） |
| `step.*` | 步进器：`reason`、`looking`（推理闸门）、`segment/segments/goal/feedback`（`odom`=按里程计，`timer`=按时间） |
| `odom.*` | `enabled/subscribed/topic/received/fresh`；位姿不新鲜时伺服会 hold |
| `servo.*` | `enabled/active/done/translation_error_m/yaw_error_deg/goal` |
| `safety.*` | 深度闸门：`enabled/blocked/reason/nearest_m/age_s/valid_pixels`… |
| `client.*` | 客户端：`state`、`episode`、`frames/requests`、`memory_flushes`（应恒为 0）、`consecutive_failures`、`healthy`、`last_note`（最近一条动作）、`executed_vx/wz` |
| `operator.*` | 能力层：`bound`、`pending_instruction`、`finished`、`consecutive_stops`、`finish_after_stops`、`safety.*` |

---

## 8. 改配置

### 8.1 `.env`（机器本地、不进包、含密钥）

```bash
cp .env.example .env
$EDITOR .env          # 必填 INTERNVLA_SERVER_URL（BASE url）
chmod 600 .env
```

| 变量 | 必填 | 作用 |
|---|---|---|
| `INTERNVLA_SERVER_URL` | **是** | 模型服务器 BASE URL（client 自己拼 `/eval_dual`） |
| `VLM_BASE_URL` / `VLM_API_KEY` / `VLM_MODEL` / `VLM_MESSAGE_FORMAT` | 语音/Pilot 用 | Pilot 的"VLM" |
| `RANGER_CAN_INTERFACE` / `RANGER_CAN_BITRATE` | 否 | 默认 `can_ranger` / `500000` |
| `TENCENT_ASR_APPID` / `TENCENTCLOUD_SECRET_ID` / `TENCENTCLOUD_SECRET_KEY` | 仅语音 | 腾讯 ASR/TTS |
| `INTERNVLA_INSTRUCTION` | — | `.env.example` 里有，**代码不读**；改指令用 `set_vln_instruction.sh` |

### 8.2 `robonix_manifest.yaml`（**真正生效**的那份）

部署路径是 `start.sh → rbnx boot → provider`，读的是 manifest 里
`internvln_navigation.config` 块，缺的键回落到包内 YAML。

常改的键（改完 `python3 scripts/validate_manifest_consistency.py`，再**重启栈**）：

| 键 | 现值 | 说明 |
|---|---|---|
| `mode` | `live`（**当前文件就是这样**，不是文档里写的 dry-run） | 用 `set_vln_mode.sh` 改，别手改 |
| `max_vx` / `max_wz` | 0.25 / 0.30 | 节点侧限幅 |
| `discrete_forward_m` / `discrete_step_deg` | 0.25 / 15.0 | 动作码的物理尺寸，模型训练值，别乱动 |
| `max_chunk_actions` | 4 | 一次答话最多执行几步（>4 截断） |
| `lookahead_m` | 1.00 | 轨迹答案的纯追踪前视距离 |
| `trajectory_stop_m` | 0.05 | 轨迹短于此 = "就在这儿" |
| `history_reset_frames` | 0 | **不要动**：非 0 会中途清服务端历史，车会丢目标 |
| `plan_hold_s` | 1.5 | 计划多久没更新就归零（= `max_plan_age_s`，两者必须相等） |
| `servo_enabled` / `servo_kp_*` / `servo_*_tolerance_*` | true / 2.0,1.5 / 0.05 m,3° | 连续伺服 |
| `odom_feedback_enabled` / `odom_*` | true | 按里程计结束每一段 |
| `guard_*` | `max_vx 0.25`、`max_wz 0.30`、`min_turn_radius 0.81`、`turn_policy clamp_yaw`、`command_timeout 0.25` | guard 的限幅必须与节点侧一致（validator 会查） |
| `safety_gate_enabled` | **false** | 深度反射闸门，2026-10-22 按操作员要求关闭 |
| `server_url` | `${INTERNVLA_SERVER_URL}` | 别写死 |
| `model_crop_size` | 384 | **必须等于服务器 `--resize_w/h`** |
| `finish_after_stops` / `finish_after_stops_min_s` | 20 / 30 | 连续 N 次 STOP 且 ≥30 s 才算任务结束 |
| `fault_after_failures` / `failure_retries` | 3 / 1 | 连续失败几次撤计划 / 每帧重试次数 |

### 8.3 包内默认值 `packages/internvln_navigation/config/internvln_navigation.yaml`

**部署路径不读它**（它只服务于 `launch/internvln_navigation.launch.py` 那条 ROS launch 路径，
且保持 `mode: dry-run` 作为 fail-safe）。看到它和 manifest 不一致是正常的，**改 manifest**。

### 8.4 热改（不用重启）

```bash
scripts/set_vln_instruction.sh "walk toward the elevator"   # 换任务
scripts/set_vln_instruction.sh stop                          # 撤任务
```

### 8.5 改了什么必须重启？

| 改动 | 生效方式 |
|---|---|
| `mode` | **重启栈**（运行中的栈不认） |
| `.env`（URL、密钥） | 重启栈（`start.sh` 每次启动重读） |
| manifest 里任何运动相关值 | 校验 + **重启栈** |
| 代码 / `capabilities/*.srv` | `bash build.sh` + 重启栈 |
| 指令 | 不用重启，`set_vln_instruction.sh` |

---

## 9. rebuild

```bash
# 容器内
cd /root/deploy
bash build.sh                                   # 全量、幂等
RBNX_SKIP_DEPS_CHECK=1 bash build.sh            # 断网/依赖已装好时跳过依赖检查

# 验产物
ls rbnx-build/codegen/robonix_mcp_types/ | grep internvln   # 期望 internvln_navigation_control_mcp.py
ls -l packages/internvln_navigation/rbnx-build/.rbnx-built

# 语音包（首次或清了 cache 才需要）
rbnx build -p rbnx-boot/cache/primitive-audio-client-bridge-rbnx --no-update-check
SPEECH_BACKEND=tencent rbnx build -p /root/workspace/services/speech --no-update-check
```

`build.sh` 内部就是 `rbnx build -f robonix_manifest.yaml`；包内构建依次跑
`rbnx codegen -p … --mcp --ros2` → 依赖检查 → colcon overlay → `colcon build --packages-select internvln_navigation`。

容器重建见 §3.3（`run_container.sh` 每次都是 停→删→跑，build 产物在挂载盘里不会丢）。
`docker commit` **本仓没有流程**，出问题先问管理员，且不要往已有 tag 上提交；镜像缺失走
`docker load -i` 或 `ROBONIX_VLN_IMAGE=<repo:tag>`。

---

## 10. 校验 / 测试

```bash
# 全量离线门（单测 + manifest 一致性）
bash scripts/validate_offline.sh

# 只校验 manifest
python3 scripts/validate_manifest_consistency.py

# 包内单测（必须在包目录下跑）
cd packages/internvln_navigation
python3 -m unittest discover -s tests -q
# 或
bash tests/run_offline_tests.sh
```

> 已知环境性红点：`test_set_vln_mode` 有 6 条在 manifest 为 `live` 且栈在跑时必红，属环境不是回归。

脚本语法扫一遍：

```bash
for f in build.sh start.sh stop.sh scripts/*.sh \
         packages/internvln_navigation/*.sh packages/internvln_navigation/scripts/*.sh; do
  bash -n "$f" || echo "SYNTAX FAIL: $f"
done
```

---

## 11. 排障速查

| 现象 | 先查 |
|---|---|
| `start.sh` 报 `atlas 0.0.0.0:50051 is taken` | 上一套栈没死。`bash stop.sh`（现在没 state 文件也会扫 boot 父进程）→ 再起 |
| `stop.sh` 说 `no boot state` 但 `pgrep` 有 provider | 同上；实在不行 `pkill -f 'internvln_navigation\.'` |
| 车不动，`dry-run prediction` 里 `vx=0` | 看 `status.client.last_note`：`awaiting_instruction` = 没下指令；`discrete_action[0]` = 模型判到了/画面不对 |
| 一直 `awaiting_instruction` | `require_instruction: true` 时不下指令模型不会被问；用 `set_vln_instruction.sh` |
| 模型连不上 | `probe_internvla_server.py`；再 `curl … /eval_dual` 期望 405 |
| 车走 S 形/错过目标 | 看 `status.command` 与 `servo.goal`；`lookahead_m` 是否被改回 0.3；`plan.reason` 是否 `expired`（刹车在切） |
| 到了不停 / 原地蹭 | `servo.done`、`step.feedback`；`trajectory_stop_m` |
| `/cmd_vel` 发布者 >1 | **立刻停**：`bash stop.sh`，查是不是导航栈也在跑 |
| 相机没图 | `lsusb \| grep -i orb`；容器重建有没有带上 `--device-cgroup-rule 'c 189:* rmw'` |
| CAN 不通 | `ip -details link show can_ranger`（要 `state UP` + `bitrate 500000`）；`./setup-hw.sh` |
| 语音没反应 | `ss -lntp \| grep 60002`；`docker logs … \| grep -i asr` |

---

## 12. 文档导航

| 文件 | 给谁看 |
|---|---|
| [docs/OVERVIEW.md](docs/OVERVIEW.md) | 原 README：它是什么、命令链怎么串 |
| [docs/COMMANDS.md](docs/COMMANDS.md) | 更细的命令速查（含每步期望输出） |
| [docs/DEPLOY.md](docs/DEPLOY.md) | 第一次上车：迁移 → build → 跑通 |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | 操作员保姆级流程 |
| [docs/SAFETY.md](docs/SAFETY.md) | 动之前必须知道的事 |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | 验收清单 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 设计与模型对接 |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | 改动台账 + 回退说明 |
| [packages/internvln_navigation/README.md](packages/internvln_navigation/README.md) | 包内模块与线程模型 |
