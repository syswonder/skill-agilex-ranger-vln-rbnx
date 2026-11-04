# skill-agilex-ranger-vln-rbnx

Ranger Mini v3 的 **InternVLA-N1（DualVLN）视觉导航**部署。

它由 `skill-follow-person-rbnx`（MiniCPM-RobotTrack 跟随）改写而来：**只把"模型那一层"换掉**——
从"跟着人走 + 固定距离"换成"听一句话，看着画面往前走"。
底盘适配层（chassis guard）、源选择器（source mux）、图像预处理、时间轴设计**原样保留**。

> 参照仓 `skill-follow-person-rbnx`、`robot-agilex-ranger_mini_v3` **一个字都没动**，本仓是独立副本。

---

## 一句话说清它做什么

给机器人一句话（"walk toward the door"），机器人把摄像头画面**持续发给远端的 DualVLN 模型**，
模型每帧回一个离散动作（前进 0.25 m / 左转 15° / 右转 15° / 停），
机器人把这个动作换算成速度，经安全钳制后发给 Ranger 底盘。

## 命令链（只有这一条）

```
DualVLN 服务器 (远端 HTTP)
    ↓  每帧一个 discrete_action 或 trajectory
InternVLAClient.evaluate()            [推理线程，慢，3~8 s 都可能]
    ↓  动作 → (vx, wz)
PlanStore                             [50 Hz 派发定时器，快]
    ↓
source mux  =  /cmd_vel_nav
    ↓
chassis guard  （Ranger 转弯半径钳制）
    ↓
/cmd_vel
    ↓
ranger_chassis  →  CAN
```

**绝不允许绕过 chassis guard 直接连 `ranger_chassis`。**

## 本仓只写一个原语；语音是可选的旁路

| 类型 | 名字 | 干什么 |
|---|---|---|
| primitive | `internvln_navigation` | 上面整条链：模型客户端 + 底盘守卫（**本仓代码**） |
| capability | `robonix/primitive/vln/driver` | Robonix 生命周期接口 |
| capability | `robonix/primitive/vln/task` | `set` / `get` / `stop` 一句话 |
| primitive（外挂） | `audio_client_bridge` | robonix-client 的麦克风/音箱，反向 WebSocket 60002 |
| service（外挂） | `speech` | 腾讯云 ASR/TTS：把说的话变成文本，把回复念出来 |

语音**不在控制链上**：没有 client 连 60002 时麦克风和音箱都是哑的，底盘行为一字不变。
它只是给 `vln/task` 多开一个"用嘴下指令"的门。

模型端**不在本仓**：它是算力服务器上的 HTTP 服务（官方那份是
`scripts/realworld/http_internvla_server.py`）。本仓只负责"怎么跟它说话"。
上车前先用 [docs/DEPLOY.md](docs/DEPLOY.md) 4.2 的 curl 确认那条 `POST /eval_dual` 在。

## 快速开始

```bash
cp .env.example .env          # 必填 INTERNVLA_SERVER_URL
python3 scripts/probe_internvla_server.py --url $INTERNVLA_SERVER_URL   # 先证明能连上
bash scripts/validate_offline.sh                                        # 离线自检，不动机器人
bash build.sh && bash start.sh                                          # 默认 dry-run，不会动
```

要它动：`scripts/set_vln_mode.sh live --yes` 然后重启。命令全表见 [docs/COMMANDS.md](docs/COMMANDS.md)。

## 三条红线

1. **默认 dry-run**：不建速度发布者，chassis guard 根本不起。要动必须 `scripts/set_vln_mode.sh live --yes` 并重启。
2. **没有避障、没有定位、没有急停**。模型只看一张图，它不知道前面有什么。本机那道
   **深度近距反射停车**（前向带 <0.30 m 直接零速度，见 `docs/SAFETY.md` §1.1）**当前已在
   manifest 里关闭**（`safety_gate_enabled: false`，2026-10-22 按操作员要求），关掉后连深度
   都不开流——现在唯一的本地保护是 chassis guard 的限速/限加速度/前向锁/命令超时。
   它就算开着也只会停、不会绕，更不能替代遥控器。
3. **模型服务器没有鉴权**。防火墙只放行机器人网段，**绝不能 `0.0.0.0/0`**。

## 文档

| 文件 | 给谁看 |
|---|---|
| [docs/COMMANDS.md](docs/COMMANDS.md) | **命令速查**：开机到收工的全部命令 + 每步期望输出 + 切 live 与实机验证顺序 |
| [docs/DEPLOY.md](docs/DEPLOY.md) | **第一次上车**：迁移 → build → 跑通 的逐步命令与期望输出 |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | 验收清单：逐条打勾，每项都要留证据 |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | 操作员：保姆级开机/测试/排障 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 开发：为什么这么设计，与模型服务怎么对接 |
| [docs/SAFETY.md](docs/SAFETY.md) | 所有人：动之前必须知道的事 |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | **改动台账**：改了什么、为什么、踩过哪些坑 |
| [packages/internvln_navigation/README.md](packages/internvln_navigation/README.md) | 包内部：模块划分与线程模型 |
