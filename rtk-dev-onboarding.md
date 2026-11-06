# RTK 容器上手指南

> Orin 开发机 · 容器 `robonix_rtk_dev`
>
> 这台 Jetson Orin 上跑着好几套机器人项目，**你只用你自己的容器 `robonix_rtk_dev`**。别人的容器、代码、底盘一律别碰。

## 先记住这五条

1. 只用 `robonix_rtk_dev`。另外三个容器是别人的项目，不 stop、不 rm、不进去乱跑。
2. 镜像里的东西是大家共用的。装软件可以在容器里装，但别重建镜像、别 `docker rmi`。
3. 代码放宿主机，别放容器里——容器随时会被删了重建，里面的文件跟着没。
4. 跑 ROS 之前先 `export ROS_DOMAIN_ID=42`，否则会和别人的话题串在一起。
5. 底盘是上电的真车。任何往 `/cmd_vel` 发速度的代码都会让轮子转，跑之前先问管理员。

---

## 1. 这台机器

| 项目 | 内容 |
|---|---|
| 主机名 / 账号 | `ubuntu` / `wangxiaofeng`（新同事的账号找管理员开） |
| 局域网地址 | `10.29.135.137`，SSH 端口 22 |
| 硬件 | Jetson AGX Orin，12 核，61 GB 内存 |
| 系统 | Ubuntu 22.04.5 + JetPack R36.5.0（L4T 5.0） |
| 磁盘 | 系统盘 `/` 只剩 6.8 GB；数据盘 `/mnt/ssd` 剩 43 GB（镜像和容器都存在这） |

> **磁盘很紧。** 大文件、数据集一律放 `/mnt/ssd` 下，别往 `/` 写，也别随手 `docker pull` 大镜像。

## 2. 登录

1. 同一局域网内直接 SSH：

   ```bash
   ssh wangxiaofeng@10.29.135.137
   ```

   密码找管理员要。别用 root 登录。

2. 传文件用 `scp`：

   ```bash
   scp ./mydata.tar wangxiaofeng@10.29.135.137:/mnt/ssd/<你的名字>/rtk/
   ```

3. 这里没有图形桌面可依赖，都是命令行操作。

## 3. 硬件脚本 setup-hw.sh

宿主机上的 `/home/wangxiaofeng/setup-hw.sh` 负责把那些"活不过重启"的硬件配置一次性恢复好：模块加载、网口地址、CAN 口名字和速率、底盘控制模式、GPU 设备。它是全车共用的，不是给你项目专用的——可以跑，但要挑时候。

### 它做哪几件事

- **雷达网口**：从物理网口里找出插着 Livox Mid-360 的那个，配上 `192.168.1.50/24`，读出雷达自己的地址并写进 `.env` 的 `LIDAR_IP`。
- **底盘 CAN**：加载 `gs_usb` 驱动（JetPack 内核默认不带，本机装了修补过的模块），把适配器对应的 CAN 口改名成 `can_ranger`、设 500 kbps 并拉起；检查控制器是不是 `ERROR-ACTIVE`、发送是不是卡死（ENOBUFS），必要时复位链路。
- **底盘控制模式**：发一帧把底盘从 STANDBY 切到 CAN 控制模式，再读心跳确认。底盘不上电时这一步会跳过。
- **相机**：按 USB 厂商号 `2bc5` 找 Orbbec，只检查，不改配置。
- **GPU**：重新生成 Docker 的 CDI 配置，让容器能拿到 Jetson 的 GPU（重启后设备节点会变）。

脚本是幂等的：已经配好的部分会直接跳过。它能自适应，不靠写死的网卡名。

### 什么时候跑

- 机器重启之后，在起任何用到硬件的服务之前；
- 雷达或底盘断电、重新上电之后；
- 拔插过 USB-CAN、相机，或者换过网口之后；
- 出现这些症状时：容器里收不到雷达数据、发底盘指令车不动、CAN 报 ENOBUFS、找不到相机、容器里没有 GPU。

### 怎么跑

```bash
cd /home/wangxiaofeng
./setup-hw.sh          # 脚本自己会 re-exec 到 sudo，会让你输密码
```

看输出和最后一行总结：`[ OK ]` 正常，`[WARN]` 需要人看一眼，`[FAIL]` 说明那一路没配上。全部 OK 再往下用硬件。

默认参数可以临时覆盖：`LIDAR_HOST_IP=192.168.1.50`、`LIDAR_CIDR`、`RANGER_CAN_NAME=can_ranger`、`RANGER_CAN_BITRATE=500000`、`ORBBEC_USB_VID=2bc5`，写成 `LIDAR_HOST_IP=… ./setup-hw.sh` 即可。

> **它写哪个 .env？** 脚本默认用自己所在目录下的 `.env`，现在就是 `/home/wangxiaofeng/.env`。如果你自己的项目也有 `.env` 要同步雷达 IP，用 `ROBONIX_ENV_FILE=<你自己的 .env 路径> ./setup-hw.sh` 指过去。

> **跑之前先确认**：它动的是全车硬件。会短暂把 `can_ranger` down 再 up——正在跑的底盘节点会掉 CAN socket，必须重启；控制器不健康时还会摘挂一次 USB-CAN；也会临时占用雷达网口的地址。所以**只在宿主机上跑，别在容器里跑；有人正在用车或正在跑栈的时候，先问一声**。

## 4. 用你的容器

容器已经起好了，名字 `robonix_rtk_dev`，基于镜像 `robonix:humble-orin-deps-slim`（Ubuntu 22.04 + ROS 2 Humble，网络是 host 模式，没有 GPU）。按下面的方法挂上你自己的代码目录后，进容器的默认工作目录就是 `/root/rtk`。宿主机上没把你加进 docker 组，所以 docker 命令都要 `sudo`；进到容器里面是 root，不用 sudo。

### 进入与退出

```bash
sudo docker exec -it robonix_rtk_dev bash   # 进入
source /opt/ros/humble/setup.bash           # 每个新 shell 都要执行一次
exit                                        # 退出（容器继续在跑）
```

容器靠一个 `tail -f /dev/null` 保活，里面是空的，不会自动起任何程序。想省掉每次 source，可以把那行写进容器里的 `~/.bashrc`——但重建容器后会丢。

### 开关与状态

```bash
sudo docker ps -a                           # 看四个容器的状态
sudo docker start robonix_rtk_dev           # 机器重启后要手动起（不自启）
sudo docker stop robonix_rtk_dev
sudo docker restart robonix_rtk_dev
```

### 装着装着发现不对劲

容器里的环境改坏了，删掉重建就行，前提是你的代码在宿主机上。重建用下面这条，把 `/mnt/ssd/<你的名字>/rtk` 换成你自己建的目录：

```bash
mkdir -p /mnt/ssd/<你的名字>/rtk
sudo docker rm -f robonix_rtk_dev
sudo docker run -d --name robonix_rtk_dev --init --hostname ubuntu --network host \
  --workdir /root/rtk \
  -v /mnt/ssd/<你的名字>/rtk:/root/rtk \
  -v /dev:/dev --device-cgroup-rule 'c 189:* rmw' \
  robonix:humble-orin-deps-slim
```

`/root/rtk` 就是你在容器里看到自己代码的地方：宿主机上改，容器里立刻生效；删掉容器重建，代码还在。加了 `--init` 是为了回收僵尸进程。

## 5. 代码放哪

- 在 `/mnt/ssd` 下建一个你自己的目录，比如 `/mnt/ssd/<你的名字>/rtk`，本体项目的代码都放这里。
- 然后用上面的重建命令把它挂到容器的 `/root/rtk`。容器里只往这个目录写代码，宿主机和容器看到的是同一份。
- 容器里除了挂载目录，写进去的东西都在容器层，重建就没了。pip / apt 装的东西同理。
- 要长期保留的环境改动，写个 Dockerfile 或 `docker commit` 之前先问管理员——镜像和磁盘是共用的。

## 6. 不要动的东西

- **别人的容器**：`robonix_ros_dev`、`robonix_follow_dev`、`robonix_internvln_dev` 分别跑着导航、跟随、internVLN 三套项目。不要 stop、rm、restart，也不要进去跑命令。
- **别人的代码**：宿主机 `/mnt/ssd` 下不是你建的那些目录，属于别的项目和别的人，别读、别改、别删。
- **镜像**：不要 `docker rmi`、`docker image prune`，也不要往现有镜像 tag 上 commit。这些镜像都是本地构建的，删了没有地方重新下载。
- **底盘**：车是上电的真车，和你的项目共用。**任何发布 `/cmd_vel` 的节点都会让轮子动。** 要跑运动相关的东西，先确认车周围没人、脚下没东西，并找管理员确认怎么用 dry-run。不要拔线、不要 `rmmod` 驱动、不要动 `/dev` 下面的设备权限。
- **主机**：不要 reboot、关机、`apt upgrade` 系统包，也不要杀不认识的进程。网络是 host 模式，你占的端口就是宿主机的端口，起服务前先 `ss -ltnp` 看一眼有没有被占。

## 7. 速查

| 想做的事 | 命令 |
|---|---|
| 登录 | `ssh wangxiaofeng@10.29.135.137` |
| 硬件自检 | `cd /home/wangxiaofeng && ./setup-hw.sh` |
| 看容器 | `sudo docker ps -a` |
| 进容器 | `sudo docker exec -it robonix_rtk_dev bash` |
| 配 ROS | `source /opt/ros/humble/setup.bash` |
| 隔离话题 | `export ROS_DOMAIN_ID=42` |
| 重启后拉起 | `sudo docker start robonix_rtk_dev` |
| 传文件 | `scp file wangxiaofeng@10.29.135.137:/mnt/ssd/<你的名字>/rtk/` |
| 看端口占用 | `ss -ltnp` |
| 看磁盘 | `df -h / /mnt/ssd` |

> ROS 默认是 `rmw_zenoh_cpp`，跨进程通信要先起 router：`ros2 run rmw_zenoh_cpp rmw_zenohd`。用你自己的 `ROS_DOMAIN_ID`，别去连别人的 router。

---

有拿不准的操作，先问管理员，别先试。最后更新 2026-10。
