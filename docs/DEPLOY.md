# 部署执行手册 · 从开发机到机器人

这一页只讲三件事：**怎么把本仓搬到机器人上 / 怎么 build / 怎么用**。
每一步都给可以直接复制的命令和**期望输出**，对不上就停在那一步。

- 安全红线：[SAFETY.md](SAFETY.md)（动手前必读）
- 日常操作（看链路、验收清单、排障）：[RUNBOOK.md](RUNBOOK.md)
- 本文档与 RUNBOOK 的关系：本文档负责"把东西装上去并第一次跑通"，RUNBOOK 负责"反复用它"。
- 改过什么、为什么、踩过哪些坑：[CHANGELOG.md](CHANGELOG.md)

---

## 0. 全局路径总表（先记这一张）

| 位置 | 路径 | 谁定的 |
|---|---|---|
| 开发机（Windows，本仓所在） | `D:\工作\北大信研院\代码\ranger\skill-agilex-ranger-vln-rbnx` | — |
| 机器人宿主机 | `/mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx` | `scripts/run_container.sh:47` **硬编码**，改这里必须同时改脚本 |
| 机器人宿主机 · Robonix 工作区 | `/mnt/ssd/wxf/jiaofu/robonix` | `scripts/run_container.sh:52` 硬编码 |
| 容器 | `robonix_vln_dev` | `scripts/run_container.sh:46` |
| 容器内看到的部署目录 | `/root/deploy` = 机器人宿主机那个目录 | 挂载，不是拷贝 |
| 容器内看到的 Robonix 工作区 | `/root/workspace` | 挂载 |

**挂载关系**（`docker run -v`，不是 copy）：

```
机器人 /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx  →  容器 /root/deploy
机器人 /mnt/ssd/wxf/jiaofu/robonix                 →  容器 /root/workspace
机器人 /dev                                        →  容器 /dev
```

所以在容器里 `bash build.sh` 产生的 `rbnx-build/` 是**直接落在机器人硬盘上的**，
容器删掉重建（`run_container.sh` 每次都会重建）**不会丢**，不用重新 build。

---

## 1. 三方前置条件（缺一个都走不下去）

### 1.1 机器人宿主机

| 需要 | 检查命令 | 期望 |
|---|---|---|
| 能 SSH 上去、有 sudo | `sudo -v` | 不报错 |
| docker 可用 | `docker ps` | 列出容器表头也不报错 |
| ROS 2 Humble 镜像已在本地 | `docker images \| grep humble-orin` | 看到一行 `robonix:humble-orin-deps-*` |
| Robonix 工作区在 | `ls /mnt/ssd/wxf/jiaofu/robonix` | 有内容（空目录会导致容器里没有 rbnx） |
| Ranger CAN 设备在 | `ls /sys/class/net \| grep can` | 有 `can_ranger` |

镜像这一条**最容易卡**：这些镜像是本地 `docker build` 出来的，**不在任何 registry 上**，
tag 写错不是"以后再 pull"，而是直接停。`run_container.sh` 第 3 步会替你拦下并列出本机实际有的镜像。

### 1.2 算力服务器（模型端，不在本仓）

本栈**不自带模型**，它只通过网络问远端。开工前必须满足：

| 需要 | 怎么确认 |
|---|---|
| DualVLN 服务在跑，端口 5801 监听 | `curl -sS --noproxy '*' --max-time 5 -o /dev/null -w '%{http_code}\n' http://<算力机IP>:5801/eval_dual` → **405** |
| 机器人能路由到那台机器 | 在**机器人宿主机**上跑同一条 curl |
| 防火墙只放机器人网段 | 规则里源地址是机器人/AGX 网段，**绝不能是 `0.0.0.0/0`** |

⚠️ **那台服务没有任何鉴权**。谁能连上 5801，谁就能让这台车动。
细则见 [SAFETY.md](SAFETY.md)。

⚠️ 服务端**只有一个路由：`POST /eval_dual`**（`InternVLA-N1-DualVLN-部署文档.md`）。
它没有 `/health`，没有 `/task/start`，也没有 `/task/report`——这三个名字是本仓
早期版本自己编的，官方 `scripts/realworld/http_internvla_server.py` 和两份本地部署
文档里都没有，打过去就是 404。本仓客户端现在也只调 `/eval_dual` 一个。

怎么判断"服务活着"：对一个只有 POST 的路由发 GET，Flask 会回 **405 + `Allow: POST, OPTIONS`**。
拿到 405 就说明路由注册上了、服务在跑；`Connection refused` 才是没起。
上车前用 4.2 的一条 curl 确认——这是**唯一**能提前发现"协议对不上"的地方，
否则你会看到一台"连得上但一步不走"的机器人。

---

## 2. 迁移：把本仓搬到机器人上

本仓**不是 git 仓库**（没有 `.git`），所以是整包拷贝，不是 `git clone`。

### 2.1 开发机上打包

```powershell
cd "D:\工作\北大信研院\代码\ranger\skill-agilex-ranger-vln-rbnx"
tar -czf ..\vln-deploy.tgz `
    --exclude=.env `
    --exclude=rbnx-build --exclude=rbnx-boot --exclude=.build `
    --exclude=logs --exclude=.run --exclude=__pycache__ --exclude=.pytest_cache `
    .
```

- 包**故意不含 `.env`**：里面有 VLM 的 API key，不该跟着压缩包到处走。到机器人上按第 3 节重建。
- 不含 `rbnx-build/` `rbnx-boot/`：那是构建产物，机器人上重新生成（本地拷过去反而会因路径/架构不同而失效）。
- 生成物在 `D:\工作\北大信研院\代码\ranger\vln-deploy.tgz`，约 **60 个文件 / 350 KB**。

### 2.2 传到机器人

```powershell
scp "D:\工作\北大信研院\代码\ranger\vln-deploy.tgz" <机器人用户>@<机器人IP>:/tmp/
```

> 本仓**不记录**任何机器人 SSH 账号密码，也不要把它们写进这里的脚本或提交记录。

### 2.3 在机器人上解包到**固定路径**

```bash
sudo mkdir -p /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx
sudo tar -xzf /tmp/vln-deploy.tgz -C /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx
sudo chown -R "$USER":"$USER" /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx
cd /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx
```

**路径必须一致**：`scripts/run_container.sh` 里 `DEPLOY=` 写死了这个目录。
换地方就得改脚本（第 6 节会说），否则容器里 `/root/deploy` 是空的。

### 2.4 落位自检（三条，全过才继续）

```bash
# 1) 文件数（不含 .env 是 59，含 .env 是 60）
find . -type f -not -path './.git/*' | wc -l

# 2) 脚本语法都能过
for f in build.sh start.sh stop.sh scripts/*.sh packages/internvln_navigation/*.sh packages/internvln_navigation/scripts/*.sh; do
    bash -n "$f" || echo "SYNTAX FAIL: $f"
done

# 3) 关键文件都在（少一个后面必炸）
ls scripts/run_container.sh packages/internvln_navigation/package_manifest.yaml \
   robonix_manifest.yaml soma.yaml urdf/ranger_mini.urdf
```

### 2.5 迁移常见错

| 现象 | 原因 | 处理 |
|---|---|---|
| 容器里 `/root/deploy` 是空的 | 解包路径和 `run_container.sh:47` 不一致 | 改脚本里的 `DEPLOY=` 或换个目录重解 |
| 容器里 `rbnx: command not found` | `/mnt/ssd/wxf/jiaofu/robonix` 不存在，docker 给建了个空目录 | 确认 Robonix 工作区在那一层，再重建容器 |
| `bash -n` 报 `\r` 之类 | 文件被 Windows 编辑器存成了 CRLF | `sed -i 's/\r$//' <文件>`（本仓脚本都是 LF） |
| 少文件 | 打包时用了图形界面压缩，漏了 `packages/.../capabilities/lib/...` 这类深层目录 | 按 2.1 的命令重打 |

---

## 3. 机器人上的 `.env`

`start.sh` 第一件事就是 `source .env`，**没有它直接拒启**。

```bash
cd /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx
cp .env.example .env
$EDITOR .env
chmod 600 .env          # 里面有密钥
```

### 3.1 每个键干什么

| 键 | 必填 | 值 | 谁读它 |
|---|---|---|---|
| `INTERNVLA_SERVER_URL` | **必填** | 算力服务器**根地址**，如 `http://192.168.1.20:5801` | `start.sh`（探 `/eval_dual`）+ manifest 的 `server_url` |
| `VLM_API_KEY` | 建议填 | OpenAI 兼容端点的 key | manifest 的 `system.pilot.vlm.api_key` |
| `VLM_BASE_URL` | 建议填 | 如 `https://newapitoken.zhengchuhai.cn/v1` | 同上（**必须 OpenAI 兼容**，`/anthropic` 端点 pilot 不认） |
| `VLM_MODEL` | 建议填 | 如 `gpt-5.6-sol` | 同上 |
| `VLM_MESSAGE_FORMAT` | 可选 | `openai` | 同上 |
| `RANGER_CAN_INTERFACE` | 可选 | 默认 `can_ranger` | `start.sh` 配 CAN |
| `RANGER_CAN_BITRATE` | 可选 | 默认 `500000` | 同上 |
| `INTERNVLA_INSTRUCTION` | 不必填 | **本仓没有任何代码读它**（`.env.example` 里那行只是示例）。真正的旋钮是 manifest 的 `instruction:`，而它现在只是一段**参考文本**：`require_instruction: true` 时不会自动开跑，要发指令见 8.3 |
| `TENCENT_ASR_APPID` / `TENCENTCLOUD_SECRET_ID` / `TENCENTCLOUD_SECRET_KEY` | 语音才要 | 从跟随栈 `.env` 拷 | `speech` 服务（ASR/TTS）。留空时只有语音不可用，导航照常 |

### 3.2 `INTERNVLA_SERVER_URL` 的三条规则

1. **只写到端口**：`http://192.168.1.20:5801`。
2. **不要带后缀**：`/eval_dual`、`/health`、`/task/start`、`/task/report` 一个都不要写——
   客户端自己拼上它唯一的那条 `/eval_dual`。写了 `start.sh` 会直接报错拦住你
   （`runbook` 第 4 步）。
3. **`127.0.0.1` 只在你把服务隧道到本机时才对**。

### 3.3 验证

```bash
# URL 格式（不联网，只校验写法）
grep -E '^INTERNVLA_SERVER_URL=' .env
# 真连一次（这一步会告诉你算力服务器通不通）
cd /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx
set -a; source .env; set +a
curl -sS --noproxy '*' --max-time 5 -o /dev/null -w 'HTTP %{http_code}\n' \
     "${INTERNVLA_SERVER_URL%/}/eval_dual"
```

期望：**HTTP 405**（`Allow: POST, OPTIONS`）——路由在，只是不收 GET。
`Connection refused` → 算力服务器没起 / 端口不对；`000` + 超时 → 防火墙或路由；
**404** → 你连上了别的东西，或者服务端不是 DualVLN。

---

## 4. 算力服务器：唯一那条路由，验一次（**上车前必做**）

### 4.1 为什么单独一节

本栈 90% 的"看起来在跑但一步不走"都出在这里：节点起来了、相机有帧、状态 `running`，
但每个请求都被服务器 4xx/5xx 拒掉。这一条 curl 半分钟就能排掉。

### 4.2 一条命令

```bash
H=http://<算力机IP>:5801
IMG=/tmp/frame.jpg            # 随便一张 JPEG，用来验协议，不用是真的相机画面

# 活着（GET 一个只收 POST 的路由 -> 405 就是活着）
curl -sS --noproxy '*' --max-time 5 -o /dev/null -w 'HTTP %{http_code}\n' \
     "$H/eval_dual"

# 真跑一帧 —— multipart：json 字段 + image 文件字段，和 internVLN/vln_query.py 一致
curl -sS --noproxy '*' --max-time 30 -w '\nHTTP %{http_code}\n' \
     -X POST \
     -F 'json={"reset":true,"instruction":"Go to the chair and stop."}' \
     -F "image=@$IMG;type=image/jpeg" \
     "$H/eval_dual"
```

`reset` 的语义就是全部状态机：**`true` = 清空历史、开一个新 episode（第一帧必带）；
`false` = 把这一帧追加到历史里**。指令**每一帧都要带**，不带服务器会 500。
没有 client id，没有 task id，服务器不记你是谁。

本栈的客户端**不再中途清历史**（`history_reset_frames: 0`，官方客户端也是从不清）：
服务端那 8 帧上下文是从"episode 第一帧到现在"里均匀抽的，而模型正是这么训的
（`internvla_n1_lerobot_dataset.py` 用同一个 linspace 从第 0 帧抽），所以这段历史就是模型
对场景、对"第一次在哪儿看见目标"的记忆。曾经每 8 帧清一次，结果是目标一离开画面模型就
既锁不住也不记得自己已经开过去了。想重新打开清理就把这个值写成 N（每 N 帧一次），
状态里的 `memory_flushes` 会跟着涨，且不算新任务（`episode` 不加一）。细节见
[ARCHITECTURE.md](ARCHITECTURE.md)。

> ✅ **指令是生效的（2026-10-30 在 10.0.105.11 上核实）**。
> 那台机器实际跑的是 `python scripts/realworld/http_internvla_server.py`（cwd `/home/xfusion/hhy/InternNav`），
> 第 45 行是
> `instruction = data.get('instruction', None) or "Turn around and walk out of this office. …"`——
> **读请求里的指令**，"Turn around…" 只是没传指令时的兜底（官方 Go2 客户端就是这种用法）。
> `depth` 也是可选的（缺了自动填全零），所以本栈只发 RGB 能跑通。
>
> 之前那条"服务端可能不读指令"的警告来自**本机镜像的旧副本**（那份是纯写死的），已作废。
> 现场复核用：
> ```bash
> grep -n "instruction = " scripts/realworld/http_internvla_server.py
> ```
> 只要看到 `data.get('instruction'`，指令通道就是通的；这时"换指令没反应/原地转"要去查模型和画面，
> 不要再怀疑这一行。
>
> 另：那台机器上**没有** `scripts/realworld/server.py`（产品版），也没有 `/task/start` 路由，
> 跑的就是演示脚本。`vln_query.py` 从本机连它没问题，但要 `--noproxy '*'`（本机有 HTTP 代理，
> 走代理会 404）。

### 4.3 怎么读结果

| 结果 | 含义 | 处理 |
|---|---|---|
| 405 + 第二条 2xx 且有 `discrete_action` | 协议对上了 | 进第 5 节 |
| 第二条报 `missing file field` / 400 | 图片字段名不对 | 字段名就是 `image`（文件名 `rgb`），见 `vln_query.py` |
| 某项 **404** | 那条路径服务器上根本没有 | 服务器版本和本客户端对不上，**模型端要补**，本仓改不了 |
| 第二条 400/500 且提到 `depth` | 那台 server 是按"RGB+深度"写的，本栈请求里只发 RGB（manifest 的 `enable_depth` 只影响本机相机开不开深度流，不是发给模型的；当前部署已设为 `false`） | 同上，模型端要适配 |
| 第二条 500 且提到 `instruction` | 少了指令字段 | 本仓客户端每帧都带指令，不该出现；出现了说明有人手改过请求 |
| 连不上 | 网络/防火墙/服务没起 | 先解决连通性 |

> 说明：本仓其它文档里写的 `python -m scripts.realworld.server` 这个名字**在官方仓库里不存在**
> —— 官方那份真实文件名是 `scripts/realworld/http_internvla_server.py`，而且它只暴露
> `POST /eval_dual` 一个路由，**没有** `/health`、`/task/start`、`/task/report`。
> 以 4.2 的结果为准，不要以任何文档里的启动命令为准。

### 4.4 与本体端必须对齐的两个值

| 服务端 | 本体端 | 不一致的后果 |
|---|---|---|
| `--resize_w 384 --resize_h 384` | manifest 的 `model_crop_size: 384` | **不报错**，只是画面构图悄悄不对，动作慢慢变差 —— 最难查 |
| 动作频率 | manifest 的 `action_step_s: 1.0` + `step_settle_s: 0.5` | 走走停停的节拍：一步标称走多久、停多久。实际时长由位移和限速定（限速只会拉长），和模型快慢无关（`plan_hold_s: 1.5` 只是看门狗） |
| — | manifest 的 `odom_feedback_enabled: true` + `odom_topic: /odom` | 一段走完不再看时间，而是看 `/odom` 有没有真的转够 15° / 走够 0.25 m。**`/odom` 没发布时不会报错**，只是每一步都退回标称时长——状态里 `step.feedback` 会一直写 `timer`，那就是没闭环 |

`scripts/validate_manifest_consistency.py` 会把本体端这几个值锁在一起，改一个必须改另一个。

> **上车前确认 `/odom` 在发**：`ros2 topic hz /odom`（底盘驱动 `ranger_base_node` 起来就有）。
> 没有它，闭环静默失效、退化成"按时间开环"，症状是"停在目标前面一截 / 开过头"，且日志
> 里没有任何错误——所以 status 里专门有 `odom.received` / `odom.fresh` 两个字段。

---

## 5. 机器人端离线自检（不碰硬件、不碰模型也能跑）

```bash
cd /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx
bash scripts/validate_offline.sh
```

它跑三件事：包内单测 → manifest 自洽检查 → 残留检查。**最后一行必须是**：

```
offline validation OK
```

期望至少看到：包内 **145 个单测**全 `ok`（`Ran 145 tests ... OK`），
manifest 检查 18 条全 `[ OK ]`，最后 `manifest consistency OK`。

只想要 manifest 那部分：

```bash
python3 scripts/validate_manifest_consistency.py
```

会逐条打印命令链、相机话题、guard 上下限、模型地址，例如：

```
[ OK ] camera wiring: front_camera camera_name -> /camera_435i/color/image_raw -> model
[ OK ] guard limits: guard 0.25 m/s <= chassis ceiling 0.5 m/s (package cap 0.25)
manifest consistency OK
```

### 5.1 探针（离线，但会连模型）

```bash
python3 scripts/probe_internvla_server.py \
    --url "$(grep -E '^INTERNVLA_SERVER_URL=' .env | cut -d= -f2-)" \
    --image /tmp/frame.jpg
```

它用**机器人本体那个客户端**打 `POST /eval_dual` 若干帧（第一帧 `reset:true`，其余 `reset:false`），
打印每帧的状态码、耗时、解析出的速度，以及原始响应体。成功时最后一行：

```
PROBE OK: the server speaks the protocol this client expects
```

- **它不碰 ROS、不碰 CAN、不碰轮子**，导航栈在跑、机器人 armed 都无所谓。
- 卡在第 1 步 → 就是 4.2 第 1 条不通。
- 第 3 步只有第 0 帧 400、第 1 帧起成功，且最后一行变成
  `PROBE OK, after 1 / eval_dual rejection(s)` → 这是**预期内**的：图片字段名是运行时谈出来的，每个进程只谈一次。
- 不给 `--image` 也能跑（合成图），用来验协议；但模型对合成图的回答是胡话，**别拿它判断模型好坏**。

---

## 6. 起容器

在**机器人宿主机**上（不是容器里）：

```bash
cd /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx
bash scripts/run_container.sh vln
```

这个脚本会：检测 usbfs major → 报相机/USB-CAN 在不在 → **检查镜像存在** → 停并删掉同名旧容器 → 重新 `docker run`。

期望输出（关键三行）：

```
== adaptive container: vln ==
  USB major       = 189  (cgroup rule: c 189:* rmw)
  Orbbec camera   = 2bc5:xxxx
  USB-CAN adapter = 1d50:xxxx
  -> robonix_vln_dev started, DeviceCgroupRules=[c 189:* rmw]
```

### 6.1 三种失败

| 输出 | 原因 | 处理 |
|---|---|---|
| `[fatal] image '...' is not on this robot.` + `(none)` | 本机没有任何 humble-orin 镜像 | 先把镜像 `docker load -i <tar>` 进来 |
| `[fatal] ...` 但下面列了别的 tag | 默认 tag 和本机不一致 | 用**列出来的那个**：`ROBONIX_VLN_IMAGE=<repo:tag> bash scripts/run_container.sh vln` |
| `[warn] Orbbec ... 未检测到` | 只是警告，容器照起 | 但第 8 步一定没有画面，先查 USB |

`run_container.sh` 每次都会**重建**容器（`stop` → `rm` → `run`），这是故意的：
既保证 `--init`（tini）在，也顺手清掉上一次遗留的僵尸进程。

### 6.2 进容器

```bash
docker exec -it robonix_vln_dev bash
```

容器里没有 GPU —— **这是设计，不是漏配**：策略跑在远端算力机上，本机不做 CUDA。
相机和 USB-CAN 是 USB 字符设备，cgroup 规则已放开。

---

## 7. 构建

### 7.1 一条命令

**在容器里**：

```bash
cd /root/deploy
bash build.sh
```

### 7.2 这条命令背后发生了什么（出事时按这张表定位）

```
build.sh                                    顶层：rbnx build -f robonix_manifest.yaml
 └─ rbnx build
     ├─ 读 robonix_manifest.yaml
     │   ├─ url: 开头的 primitive（robot_description / front_camera / ranger_chassis）
     │   │        → clone 到 rbnx-boot/cache/<repo>/ 再各自 build
     │   └─ path: 开头的 primitive（internvln_navigation）
     │            → 直接用 rbnx-build/../packages/internvln_navigation 这个目录
     └─ 读 packages/internvln_navigation/package_manifest.yaml 的 `build: bash build.sh`
         └─ bash packages/internvln_navigation/build.sh
             ├─ 检查 rbnx 在 PATH、/opt/ros/humble/setup.bash 在
             ├─ rbnx codegen -p <包> --mcp --ros2      ← 生成三个 codegen 产物
             ├─ source scripts/build_robonix_ros2_overlay.sh（共享 overlay 构建器）
             ├─ bash scripts/ensure_deps.sh            ← ★ 环境依赖检查/安装就在这一步
             ├─ colcon build（lifecycle overlay + 本包）
             └─ 成功则写 rbnx-build/.rbnx-built
```

**关键点**：`rbnx` 自己**不检查也不安装任何依赖**（它只确认 rbnx 在、ROS 的 `setup.bash` 在）。
所以依赖检查是本包显式做的，就是 `packages/internvln_navigation/scripts/ensure_deps.sh`，
由 `build.sh` 第 40 行调用。

### 7.3 `ensure_deps.sh` 的三种输出

它先逐个 `python3 -c "import <模块>"`，**已有就跳过**，缺了才装（只装缺的那些）：

| 输出 | 含义 | 下一步 |
|---|---|---|
| `[internvln_navigation/deps] all 7 runtime deps importable, nothing to install` | 容器环境完整，一个包都没动 | 正常，继续 |
| `[internvln_navigation/deps] missing: cv2` + `installing: ...` + `done` | 缺的已装上（`apt-get install -y --no-install-recommends`） | 正常，继续 |
| `[fatal]`/`still not importable after install` | 装不上（容器离线 / 装完仍 import 不了） | 见 7.5 |

7 个被检查的模块：`cv2 numpy rclpy cv_bridge geometry_msgs sensor_msgs std_msgs`。
判据是**能不能 import**，不是装了哪个 apt 包。

- 非 root 时**不会**用 sudo 提权，直接报错退出（容器里本来就是 root，不是 root 说明环境不对）。
- 完全不想让它检查：`RBNX_SKIP_DEPS_CHECK=1 bash build.sh`（离线/气隙环境用）。

### 7.4 构建成功的判据（两个都必须看）

```bash
# 1) codegen 产物在（少一个 provider 就起不来）
ls rbnx-build/codegen/robonix_mcp_types/ | grep internvln
#    期望：internvln_navigation_control_mcp.py

# 2) 包自己 build 过
ls -l packages/internvln_navigation/rbnx-build/.rbnx-built
```

`start.sh`（包内的）会检查 5 个产物，缺任何一个都会直接告诉你缺哪个：

```
/opt/ros/humble/setup.bash
packages/internvln_navigation/rbnx-build/codegen/ros2_idl/install/setup.bash
packages/internvln_navigation/.build/ros/install/setup.bash
packages/internvln_navigation/rbnx-build/codegen/proto_gen/atlas_pb2.py
packages/internvln_navigation/rbnx-build/codegen/robonix_mcp_types/internvln_navigation_control_mcp.py
```

### 7.5 构建排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `rbnx is not available.` | 容器里没有 rbnx | `/root/workspace` 挂载不对（见 2.5） |
| `Missing /opt/ros/humble/setup.bash` | 镜像不是 ROS Humble 那个 | 镜像 tag 错了，回第 6 节 |
| `python3 -m pip install --user grpcio-tools` | `rbnx codegen` 缺 `grpc_tools`（这是 rbnx 唯一会自己查的依赖） | 按提示装，或在镜像里预装 |
| `colcon: command not found` | 镜像缺 colcon | 同上，镜像问题 |
| `install failed -- this container is probably offline.` | 缺依赖且装不上 | 在有网的机器上装好，或直接固化进镜像 |
| `removing stale generated ROS system-interface overlay` | 正常提示 | 不是错误，overlay 重建 |
| 编译 Python 报语法错 | 解释器太老（本包用了 `tuple[float, ...]` 之类的 3.9+ 语法） | 容器里必须是 Python 3.10+（Humble 自带就是） |

构建是**幂等**的：改完代码再 `bash build.sh` 一遍即可。改了 `.srv`/能力契约才需要留意 codegen 产物是否更新（看 7.4 第 1 条）。

---

## 8. dry-run：第一次跑通链路（机器人不会动）

### 8.1 起栈

**在容器里**：

```bash
cd /root/deploy
bash start.sh
```

`start.sh` 会依次：读 `.env` → **校验 `INTERNVLA_SERVER_URL` 写法** → 探一次 `/eval_dual` →
检查 manifest 是不是 `live` → 配 CAN → 起 zenoh router → `rbnx boot`。

期望输出：

```
DualVLN server http://192.168.1.20:5801: ... （health 返回体）
configuring Ranger CAN can_ranger at 500000 bps      ← 已经是 UP 就不会打印这行
```

| 输出 | 含义 |
|---|---|
| `INTERNVLA_SERVER_URL must be the server BASE url, not an endpoint` | `.env` 里带了 `/eval_dual` 之类的后缀 |
| `Refusing to arm (mode: live) with no model to ask` | `mode: live` 但探不到模型 —— **故意拒启** |
| `... is NOT reachable (...); dry-run continues, but no frame will produce a usable action.` | dry-run 下只是警告，但那一刻链路是死的 |
| `WARNING: mode is live -- ... WILL drive the wheels.` | 已经 armed 了，见第 9 节 |

### 8.2 看链路（另开一个终端）

```bash
docker exec -it robonix_vln_dev bash
source /opt/ros/humble/setup.bash && export RMW_IMPLEMENTATION=rmw_zenoh_cpp
cd /root/deploy && python3 scripts/watch_vln_chain.py 2>/dev/null
```

输出长这样（**这是 dry-run 的真实样子**），逐列看：

```
VLN command chain   14:22:07   mode=dry-run   (Ctrl-C 退出)

topic                        pub sub  Δmsg        vx        wz  note
------------------------------------------------------------------------------------------------
/ranger/vln/cmd_vel_raw        0   1     0         -         -  dry-run: no velocity publisher by design
/cmd_vel_nav                   0   1     0         -         -  dry-run: no velocity publisher by design
/cmd_vel                       0   2     0         -         -  dry-run: no velocity publisher by design

/ranger/vln/status  (node status, 12 msgs)
    age  0.4s  mode=dry-run  source=vln  plan=active_plan  state=running  episode=1  frames=180  flushes=8  failures=0  healthy=True
    instruction pending=go to the chair and stop  active=go to the chair and stop  reset_pending=False
    executed vx=0.0 wz=0.0  (what the node thinks it sent)
    lockstep=False  step=servo  segment=0/0  goal=-  feedback=timer  looking=yes  (nominal 1.0s + 0.5s settle)
    odometry: fresh
    servo: chasing (1.24, 0.08, 28.0deg)  err=0.214m dyaw=-2.5deg
    last_note: discrete_action[2] -> 1 step(s) over 1.00 s: turn left 15.0 deg over 1.00 s
```

**刚起栈的时候不是这个样子。** 默认 `require_instruction: true`：**没人在 8.3 里发指令之前，
节点一次模型都不问**，`frames=0`、没有 `episode`、`last_note` 是
`awaiting an operator instruction (no request sent)`，`armed=False`。上面这段是**发过指令之后**的样子。
看不到 `frames` 在涨不是坏了，是还没人下单——这是故意的（见 8.3 和第 9 节）。

**dry-run 下三行 `pub` 全是 0，这是设计如此，不是节点没起来。** 表头的 `mode=` 就是判据：
节点故意不建速度发布者、guard 也故意不启动，所以没有任何进程能发出速度。

| 看什么 | 正常 |
|---|---|
| `/ranger/vln/status` 的 `age` / `msgs` | 一直很小 / 在涨 → 节点活着 |
| `/ranger/vln/status` 的 `healthy` / `failures` | 发过指令后是 `healthy=True`、`failures=0` |
| `/ranger/vln/status` 的 `armed` | 没发指令时必须是 `False`（这时不该有请求） |
| `/ranger/vln/status` 的 `last_note` | 发指令后每隔几秒变一次 → 模型回的**动作码真的在换**（每集只写最后一帧，不会每帧变） |
| 三行 `pub` | **dry-run 下必须全是 0**（谁都不能发速度） |
| `pub=1` / `Δmsg` 在涨 | **切到 `mode: live` 之后才成立**，见 `docs/ACCEPTANCE.md` |

没发指令时 `frames` 不动是正常的；发过指令后 `frames` 还不涨 → 相机没帧
（`ros2 topic list | grep image`）。
`last_note` 一直不变但 `frames` 在涨 → 服务器在，但每帧给的同一个动作码。
想看每帧的换算结果，看 `start.sh` 终端的 `dry-run prediction: vx=… wz=…`。

### 8.3 发一条指令

**链路是"你下单它才动"**：默认配置下，指令没来之前节点不请求模型、也不发速度。发指令就是开机后
的第一件正经事：

```bash
scripts/set_vln_instruction.sh "go to the chair and stop"
```

**立刻返回**（只入队），并打印当前状态。回到 watcher 终端，应看到 `armed` 变 `True`、
`episode=` 加一、`frames` 从 0 开始涨。

指令请写成**有终点的任务**（"走到椅子前停下"），不要写"一直往前走"这类口号：DualVLN 是到达型
模型，它拿这句话判断"到了没"，没有目标物的句子会被它当成"已经做完了"而回 `STOP`。

另一扇门（Robonix 能力，`rbnx` 可用处）：

```bash
rbnx ask -c robonix/primitive/vln/task '{"operation":"set","instruction":"go to the chair and stop"}'
rbnx ask -c robonix/primitive/vln/task '{"operation":"get"}'
rbnx ask -c robonix/primitive/vln/task '{"operation":"stop"}'
```

`set` 会**阻塞到这一轮任务结束**才返回（`status: finished` / `stopped`；超过 `set_block_timeout_s`
（默认 900 s）仍没结束则回 `running`）。这样 plan 节点活多久 = 任务活多久，Pilot 不会在任务中途
反复重派；任务结束后它能拿到 `finished` 自己收尾。想看中间状态用 `get`（永不阻塞）。
`get` 在没发指令（或 `stop` 之后）时回 `status: awaiting-instruction` / `armed=False`；
模型一直卡在 STOP（连续 ≥ 20 帧且持续 ≥ 30 s）后回 `status: finished` / `running=False`
（计划已撤、不再请求模型，相当于模型自己按了 `stop`，发新指令重新开始）。
前向深度闸门关闭时 `get` 回 `status: blocked` / `running: false`：原语已被停掉，worker 不再请求模型；
这时 `set` 回 `status: held`，闸门放行后自动开始。
`stop` 不只是"退休这一条计划"：它同时把任务收走，之后节点不再请求模型，车停住等你下一条指令。

### 8.4 dry-run 验收清单

- [ ] `offline validation OK`
- [ ] `PROBE OK`
- [ ] 容器起来时打印了相机和 USB-CAN 的 ID
- [ ] `bash build.sh` 结束于 `[internvln_navigation/build] done`
- [ ] `rbnx-build/codegen/robonix_mcp_types/internvln_navigation_control_mcp.py` 在
- [ ] `/ranger/vln/cmd_vel_raw` 的 `Δmsg` 持续增长
- [ ] `/cmd_vel` 的 `pub == 0`
- [ ] `/ranger/vln/status` 里 `failures=0`、`healthy=True`
- [ ] 发 `stop` 后 `plan` 不再是 `active_plan`

**到这一步链路已经全通，而机器人一步没动。先把这一步跑稳再谈 armed。**

---

## 9. armed（会动，手不离遥控器）

### 9.1 前置（一条都不能省）

- [ ] 导航栈**已停**（本栈独占 `can_ranger` 和 `/cmd_vel`，两套不能同时跑）
- [ ] 手里有实体遥控器
- [ ] 前方 10 m 净空，有人看着
- [ ] 第 8 节全绿

### 9.2 三步

```bash
# 1) 停栈
bash stop.sh

# 2) 切模式（脚本只改这一处，原子写回；不带 --yes 会被拒）
scripts/set_vln_mode.sh live --yes

# 3) 自检（输出里应出现 (ARMED) 提示）
python3 scripts/validate_manifest_consistency.py

# 4) 起
bash start.sh        # 会打印 WARNING: mode is live ...
```

`set_vln_mode.sh` 的四种拒绝：`3` = 没带 `--yes`；`4` = **导航栈还在跑**（一个 CAN 只能有一个主人）；
`1` = manifest 里 `mode:` 不止一处（它不猜哪个是开关）；`2` = 参数写错。
不带参数（或 `status`）则只报告当前模式与有没有东西在跑，不做任何修改。

### 9.3 armed 验收清单

从 [RUNBOOK.md](RUNBOOK.md) 第 6 节抄过来，逐条打勾：

- [ ] `/cmd_vel` 的 `pub` **恰好为 1**（chassis guard 独占）
- [ ] `/cmd_vel_nav` 的 `pub` **恰好为 1**（source mux 独占）
- [ ] `/ranger/vln/cmd_vel_raw` 的 `Δmsg` 持续 > 0
- [ ] `|vx| ≤ 0.25`、`|wz| ≤ 0.30`（超了说明 guard 没生效）
- [ ] `vx` 从不为负（前向锁）
- [ ] 断开模型服务器 → **3 帧之内机器人停**，`failures` 涨到 3
- [ ] `stop` 指令 → 一个 `plan_hold_s` 之内停
- [ ] `Ctrl-C` 掉 stack → 机器人立刻停
- [ ] 重启栈 → 默认回到 dry-run（除非你又改了 manifest）

### 9.4 三层急停

| 层 | 怎么做 | 效果 |
|---|---|---|
| 软停（最轻） | `scripts/set_vln_instruction.sh stop` | 撤销计划 + **把任务收走**：节点不再请求模型，一个 `plan_hold_s` 内停住并一直等新指令（服务器无任务可作废） |
| 硬停 | `Ctrl-C` 掉 `start.sh` | 节点 close 时先发零速度再退出 |
| 物理 | 遥控器 / 拍下电门 | **永远保留这一层** |

---

## 10. 收工与回滚

```bash
# 停栈（容器里）
bash stop.sh
```

它做两件事：`rbnx shutdown -f robonix_manifest.yaml`；然后 `pgrep` 一遍
`internvln_navigation.*` —— **如果它打印 WARNING，说明有进程活过了 shutdown**，先杀掉再起导航栈。

回滚到安全状态（走人前做）：

1. `bash stop.sh`
2. `scripts/set_vln_mode.sh dry-run`（安全方向，不需要确认）
3. `python3 scripts/validate_manifest_consistency.py`（确认不再提示 ARMED）
4. 需要的话 `docker stop robonix_vln_dev`（容器停下，挂载的目录和构建产物都还在）
5. 起导航栈前确认 `can_ranger` 空闲

模型服务器在远端，本仓不管它 —— 停不停随你。

---

## 11. 故障排查总表

| 阶段 | 现象 | 先看 | 常见原因 |
|---|---|---|---|
| 迁移 | 容器里 `/root/deploy` 空 | `ls /root/deploy` | 解包路径 ≠ `run_container.sh:47` |
| 迁移 | 容器里没 `rbnx` | `ls /root/workspace` | Robonix 工作区没挂上 |
| 容器 | `[fatal] image ... is not on this robot` | 脚本列出的 tag | 镜像 tag 不对，用 `ROBONIX_VLN_IMAGE=` 覆盖 |
| 构建 | `rbnx is not available` | 容器内 `which rbnx` | 挂载/镜像问题 |
| 构建 | `still not importable after install` | `python3 -c "import cv2"` | 容器离线，或不只缺一个包 |
| 构建 | codegen 没产出 mcp | `ls rbnx-build/codegen/` | 契约文件 `.srv` 缺，或 codegen 报错被忽略 |
| 启动 | `INTERNVLA_SERVER_URL is not set` | `.env` 在不在 | `.env` 没建/没 source |
| 启动 | `must be the server BASE url` | `.env` 值 | 带了端点后缀 |
| 启动 | `Refusing to arm ... no model` | 第 4.2 条 curl | 模型服务器没起/不通 |
| 启动 | `missing .../rmw_zenohd` | 容器里该路径 | 镜像缺 `ros-humble-rmw-zenoh-cpp` |
| 运行 | 起来但 `frames=0` | `ros2 topic list \| grep image` | 相机话题不对 / 相机没插 |
| 运行 | `healthy=False`，有 `last_error` | 状态行的 `last_error` | 服务器挂了 / URL 错 / 防火墙 |
| 运行 | 状态行完全不动 | 状态行 `age` | 节点没起来，看 `logs/` |
| 运行 | 原地打转不前进 | `/cmd_vel` 那行 `vx=0 wz≠0` | 正常且各有出处：模型发动作码左/右转（纯原地转）、轨迹被判"比底盘最小半径紧"、或 `clamp_yaw` 钳制。看 `last_note` 分清 |
| 运行 | 完全不动，`/cmd_vel` 有非零值 | `ip -details link show can_ranger` | CAN 没配好 / 底盘没上电 |
| 运行 | 指令发了没反应 | 状态行 `pending=` | RPC 的 `set` 会阻塞到任务结束（`set_block_timeout_s`）；看 10 Hz 的 `_instruction` 泵有没有跑 |
| 急停 | 停不下来 | — | 遥控器；然后 `Ctrl-C`；最后 `bash stop.sh` |

抓现场（三份一起给开发看）：

```bash
docker exec robonix_vln_dev bash -lc 'cd /root/deploy && tail -200 logs/*.log 2>/dev/null'
docker exec robonix_vln_dev bash -lc 'ros2 topic echo --once /ranger/vln/status std_msgs/msg/String'
docker exec robonix_vln_dev bash -lc 'ros2 topic list'
```

---

## 12. 一页速查（复制粘贴版）

> 更完整的一页（每步的期望输出、切 live、实机验证顺序）在 [COMMANDS.md](COMMANDS.md)。
> 注意本机**没有**默认镜像 tag，`run_container.sh vln` 要用 `ROBONIX_VLN_IMAGE=` 覆盖（见 §6.1）。

```bash
# ===== 机器人宿主机 =====
cd /mnt/ssd/wxf/jiaofu/skill-agilex-ranger-vln-rbnx
bash scripts/validate_offline.sh                     # 离线自检，最后一行 offline validation OK
python3 scripts/probe_internvla_server.py --url "$(grep -E '^INTERNVLA_SERVER_URL=' .env | cut -d= -f2-)" --image /tmp/frame.jpg
ROBONIX_VLN_IMAGE=robonix:humble-orin-follow bash scripts/run_container.sh vln
docker exec -it robonix_vln_dev bash

# ===== 容器内 =====
cd /root/deploy
bash build.sh                                        # 构建（含依赖检查）
ls rbnx-build/codegen/robonix_mcp_types/ | grep internvln
bash start.sh                                        # dry-run 起栈
scripts/set_vln_instruction.sh "go to the chair and stop"
scripts/set_vln_instruction.sh stop
python3 scripts/watch_vln_chain.py 2>/dev/null        # 另开终端看链路
python3 scripts/validate_manifest_consistency.py
bash stop.sh

# ===== armed（手不离遥控器）=====
# scripts/set_vln_mode.sh live --yes   →  mode: dry-run 改成 live，然后
python3 scripts/validate_manifest_consistency.py && bash start.sh
# 走人前：scripts/set_vln_mode.sh dry-run
```

---

## 附：本仓结构（对照着找东西）

```
skill-agilex-ranger-vln-rbnx/
├── robonix_manifest.yaml        # 部署清单：起哪些 primitive、参数、system 服务
├── build.sh / start.sh / stop.sh# 顶层三个入口（转手给 rbnx）
├── .env / .env.example          # 机器本地配置（.env 不进包、不进库）
├── soma.yaml / urdf/            # 机器人描述
├── scripts/
│   ├── run_container.sh         # 起容器（宿主机）
│   ├── validate_offline.sh      # 离线门禁
│   ├── validate_manifest_consistency.py
│   ├── probe_internvla_server.py# /eval_dual 探针
│   ├── watch_vln_chain.py       # 链路观察
│   ├── set_vln_instruction.sh   # 发一条指令
│   └── build_robonix_ros2_overlay.sh
├── docs/                        # 本页 + RUNBOOK / SAFETY / ARCHITECTURE / ACCEPTANCE / CHANGELOG
└── packages/internvln_navigation/
    ├── package_manifest.yaml    # build / start 命令 + 能力声明
    ├── build.sh / start.sh      # 包自己的构建与启动
    ├── scripts/ensure_deps.sh   # ★ 环境依赖检查/安装（只装缺的）
    ├── capabilities/            # 能力契约（driver / task）+ VlnTask.srv
    ├── config/internvln_navigation.yaml
    ├── internvln_navigation/    # 代码本体（无 ROS 的纯逻辑 + ROS 壳）
    └── tests/                   # 全部离线可跑
```

构建产物（**不进包、可重新生成**）：`rbnx-build/`、`rbnx-boot/`、`.build/`、`logs/`、`.run/`。

---

## 附：本文档的可信度

| 内容 | 状态 |
|---|---|
| 离线自检（包内单测 + manifest 自洽） | 已在开发机上跑通 |
| 容器、构建、起栈、`/eval_dual` 探针 | **真机上一次都没跑过** —— 第一次按本页顺序来，哪一步卡住就停在那一步 |
| `guard_min_turn_radius_m = 0.81` | 来自 Mini 3.0 手册，**不是实测**，上车后重标 |
| 算力服务器的 `POST /eval_dual` | 见 4.2 的 curl，**以实机结果为准**；另三个接口本服务端不存在，已删 |
