# 安全红线 · Ranger InternVLA-N1

**这一页比其它所有文档都重要。动之前读一遍，每次上电前扫一眼。**

---

## 1. 这套东西不会保护机器人

| 有 | 没有 |
|---|---|
| 计划过期自动归零（`plan_hold_s`） | ❌ 避障（没有雷达、没有代价地图；下面那道闸只会停，不会绕） |
| 连续 3 次失败 → 立刻撤销正在执行的计划 | ❌ 定位（机器人不知道自己在哪） |
| chassis guard 限速 / 限加速度 / 前向锁 / 转弯半径钳制 | ❌ 急停按钮（硬件上也许有，但软件链路上没有任何一环） |
| **前向近距反射停车（深度闸门，见 §1.1）**——**当前部署已关闭，见下方红框** | ❌ 反向（DualVLN 没有后退动作，guard 也锁了前进） |
| 远端模型不可达 → 机器人停 | ❌ 人会闪避（模型只看到一帧画面） |
| 任何异常路径最终都收敛到零速度 | — |

> ⚠️ **2026-10-22 起，本部署把深度闸门关掉了**（`robonix_manifest.yaml` 里
> `safety_gate_enabled: false`，相机也相应 `enable_depth: false`）。**当前没有任何近距停车
> 反射**：模型发什么，只要没超 chassis guard 的限速/加速度/前向锁，就会直接到轮子。
> 下面是它开着时的行为，留着是因为"关"是一个词的事，重新打开后原样生效。
> 关掉的原因见 [CHANGELOG](CHANGELOG.md)；安全后果：**现场必须有人、手不离遥控器**。

**DualVLN 是一个视觉策略，不是安全系统。** 它输出的是"我觉得该往哪走"，不是"这里安全"。

### 1.1 深度闸门：一道只会停的反射，不是避障

2026-10-21 加的。`internvln_navigation` 订阅深度图（`depth_topic`），在
**画面中间 60% 宽、25%~80% 高**的前向带里取第 1 百分位最近的深度；距离小于
`safety_gate_stop_m`（默认 **0.30 m**）就**直接发布零速度**，不管模型刚才回的是箭头、
像素点还是轨迹。要回到 `safety_gate_release_m`（默认 **0.50 m**）以外才放行——两个阈值之间
有 20 cm 的回差，否则车会在 0.29/0.30 之间反复开关、一顿一顿地蹭。

**它是"闭锁"（fail-closed）的**：

| 情况 | 结果 |
|---|---|
| 深度帧正常且前方干净 | 放行 |
| 前向带有东西 < 0.30 m | 停车（`reason=obstacle`） |
| 根本没有深度帧 | **停车**（`reason=no_depth`） |
| 深度帧超过 0.30 s 没更新 | **停车**（`reason=stale_depth`） |
| 帧里可用像素太少 / 全是 0 / 格式不对 | **停车**（`reason=invalid_depth`） |

也就是说：**开 live 之前，必须先把深度流弄通，否则车一步都不会动**。这是故意的——
"不动"是部署问题，"撞墙"是安全事故。真要在没有深度的环境里裸跑，才把
`safety_gate_require_depth` 设成 `false`（默认 `true`，不建议）。

**它不做什么：**

- 不绕障：只在原地停住，等东西走开（闸门自动放行）或操作员发 `stop`；
- **不自己重启被它停掉的任务**：闸门一关，正在跑的任务就被撤销了（见下面 §1.2），
  放行后机器人保持"等指令"状态，要动得重新发一条。挡住时发的指令是个例外——
  它会被 `held` 住、放行后自动开始；
- 不看侧面和后面：只保护前向带；侧向擦碰它管不了；
- 不替代遥控器：低矮/透明/反光物体（玻璃、镜面、深色地毯边缘）深度可能测不到，
  **第一次 live 仍然要人在旁边、手不离遥控器**；
- 数据只在 dry-run 里可验证：`/ranger/vln/status` 的 `safety` 字段会给出
  `blocked` / `reason` / `nearest_m` / `age_s`。**dry-run 下用手在前向带里晃一下**，
  看 `blocked` 翻成 `true`、`dry-run prediction` 里 `vx=0 wz=0`，就算验收通过——
  全程机器人不会动。

### 1.2 闸门关闭 = 原语被停掉，而不只是速度归零

2026-10-21 加的。以前闸门只是把发出去的速度清零，Robonix 那边完全不知道：任务还在、
`vln/task` 还报 `running`，推理 worker 还在每帧问模型——车被挡住时远端一直在被请求，
看起来就像"一直循环请求 VLN"。

现在闸门一关（`blocked` 从 `false` 变 `true` 的那一 tick），节点通过 `VlnRuntime` 做两件事：

1. `plan_store.clear()`：正在执行的计划立刻作废，速度归零；
2. `client.clear_instruction("safety gate closed: …")`：**解除武装**。这一步才是关键——
   只有它能让推理 worker 停下来，不再发 `/eval_dual`。

随后 `robonix/primitive/vln/task` 的 `get` 报：

| 字段 | 值 |
|---|---|
| `status` | `"blocked"` |
| `running` | `false` |
| `detail` | `reason=… nearest=… stop=… blocked_for=…s stops=…; no model requests are being made; …` |

挡住期间 `set` 一条新指令不会启动：返回 `status="held"`，指令被记住、**不武装**。
闸门放行后它自动开始（`episode` 加一，`frames` 重新涨），不需要重发。

闸门放行**不会**让刚才被撤销的那个任务自己恢复——机器人回到"等指令"。
这是故意的：安全停过一次以后要不要继续，由人决定，不由反射决定。

dry-run 里同样生效（dry-run 也请求模型，这条规则就是冲它去的），
`/ranger/vln/status` 的 `operator.safety` 字段能直接看到 `blocked` / `stops` / `held_instruction`。

调参只动 manifest 里那几个 `safety_gate_*`：停车距离跟着**车头到障碍物的真实距离**
量一次再定，别照抄默认值；ROI 上下边界要保证能看到墙、又不要把眼前的地板圈进来
（地板永远在一米内，圈进来就永远停）。

## 2. 三条硬规则

### 规则一：先 dry-run，永远

`robonix_manifest.yaml` 里 `mode: dry-run` 是出厂值。
切模式用 `scripts/set_vln_mode.sh`（`live` 要带 `--yes`），**不要手改文件**——
脚本会顺带挡住"导航栈还在跑就想武装"这类错误。

dry-run 下：
- 节点**不创建任何 Twist 发布者**（源码级保证，`tests/test_static_contract.py` 会检查）；
- chassis guard **不启动**；
- 但**照样订阅画面、照样订阅深度、照样请求远端模型、照样打印预测速度**，深度闸门的判定
  也照常算出来发到 `/ranger/vln/status` 的 `safety` 字段里。

也就是说 dry-run 就是"完整跑一遍，只是手不放在方向盘上"。先用它把链路和模型都验通。

### 规则二：第一次 live，手不离遥控器

`mode: live` 需要**改文件 + 重启**，不是运行时可切的开关——故意的，防止手滑。

改文件用 `scripts/set_vln_mode.sh live --yes`（不带 `--yes` 会被拒；**导航栈还在跑也会被拒**，
因为一个 CAN 只能有一个主人）。脚本只做这一处编辑，**不会**顺手把栈起起来。

第一次 live 时：
1. 遥控器拿在手上，随时能按停；
2. 地面清空，机器人前方至少 10 m 无人无物；
3. 一次只发**一条**指令，别连着发；
4. `scripts/watch_vln_chain.py` 开着，盯着 `/cmd_vel` 那一行；
5. 先在 dry-run 里确认 `status.safety.reason` 在干净地面是 `clear`、手伸进前向带后变成
   `obstacle` 且 `nearest_m` 合理（深度流没通时它会一直 `no_depth`，live 下等于车不动）。

### 规则三：一个 CAN 总线只能有一个主人

`ranger_chassis` 独占 `can_ranger`，`/cmd_vel` 只允许 chassis guard 一个发布者。

```bash
# 启动本栈之前，确认导航栈已经停了
rbnx shutdown -f /path/to/robot-agilex-ranger_mini_v3/robonix_manifest.no-arm.yaml
```

两套栈同时跑 = 两套 PID 抢一个 CAN = 无法预测的运动。

## 3. 远端模型服务器：没有鉴权

算力服务器上的推理服务（官方那份叫 `scripts/realworld/http_internvla_server.py`）默认**绑定 `0.0.0.0` 且没有任何认证**。

- 谁能连上它，谁就能让机器人动起来；
- 防火墙规则**必须**限定机器人/AGX 网段；
- **绝对不要**写 `0.0.0.0/0`、不要做端口映射到公网、不要挂在有外网入口的网段上；
- `--port 5801` 不要改成一个会被人扫到的位置。

如果必须在不可信网络上跑：用 SSH 隧道把 5801 转发到机器人本机，再把 `INTERNVLA_SERVER_URL` 写成 `http://127.0.0.1:5801`。

## 4. 部署文档里的明文口令

`robot-agilex-ranger_mini_v3` / `skill-follow-person-rbnx` 的部署文档里有明文 SSH 口令
（GO2 `unitree`/`123`、Lite3 `user`/`123456`、Lynx S10 `user`/`'`）。
**不要把那些文档抄进本仓或本仓的 commit。** 本仓不需要它们。

## 5. Robonix provider 会自动广播自己的局域网 IP

provider 的 socket 绑 `0.0.0.0` 并自动对外 advertise——这是设计如此
（robonix-client 要从远端 PC 直连 provider）。绑回环会让远端全部 `connection refused`，
而本地自检却全部通过。

知道这件事就行：**机器人所在的局域网不是一个可信网络**。

## 6. 失败行为速查

出问题时机器人应该**停**，而不是乱走。下面每一条都对应代码里的一处设计。

| 情况 | 结果 | 靠什么 |
|---|---|---|
| 远端服务器连不上（1~2 帧） | 那一帧没有新计划，旧计划到期归零 | `PlanStore` 的 `max_plan_age_s` |
| 远端服务器连不上（连续 3 帧） | 立刻撤销**正在执行**的计划 → 停 | `fault_after_failures` + `ModelUnavailableError` |
| 模型回了看不懂的东西 | 这一帧作废，算一次失败 | `ProtocolError` |
| 相机断流 | 没有新帧 → 没有新计划 → 归零 | worker 只在有新帧时请求 |
| 前向带出现 < 0.30 m 的物体 | **立刻零速度**；计划撤销、客户端解除武装 → `vln/task` 报 `blocked`，不再请求模型 | 深度闸门 + `VlnRuntime.set_safety_state` |
| 深度断流 / 帧超过 0.30 s / 帧不可用 | **停**（闭锁；`safety` 里能看到 `no_depth` / `stale_depth` / `invalid_depth`），同样撤销任务、解除武装 | `safety_gate_require_depth` |
| 画面不是 JPEG | 直接丢弃，不发出去 | `build_multipart_body` |
| 底盘超过 0.25 s 没收到命令 | guard 主动发零 | `guard_command_timeout_s` |
| 上位机 50 Hz 派发线程卡死 | 底盘固件自己 500 ms 后切断 | AgileX firmware watchdog |
| 操作员发了新指令 | 在途的旧结果**丢弃**，不回底盘 | 代际计数（generation） |
| 操作员按 `stop` | 撤销计划（服务器无任务可作废） | `vln/task stop` 或 ROS 话题发 `stop` |

**注意**：所有"停"都收敛到**零速度**，不会出现"停不下来"或"反向乱走"。

## 7. 上电前的 60 秒检查

```bash
bash scripts/validate_offline.sh                      # 离线自检（不碰机器人）
python3 scripts/probe_internvla_server.py --url $INTERNVLA_SERVER_URL   # 模型通不通
scripts/set_vln_mode.sh                               # 现在是什么模式、有没有东西在跑
```

然后确认：
- [ ] 导航栈已停（CAN 空闲）；
- [ ] `mode:` 是 `dry-run`（除非你已经准备好看它动）；
- [ ] 深度话题有数据、`status.safety.reason` 不是 `no_depth` / `stale_depth`；
- [ ] 前方净空，遥控器在手；
- [ ] 用的是**真实相机画面**，不是合成图（合成图只会让模型胡说）。
