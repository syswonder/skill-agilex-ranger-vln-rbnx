#!/usr/bin/env bash
# Ranger Mini v3 hardware interface bring-up (run on the HOST, not in the container).
#
# Recreates the host-side interface configs that do NOT survive a host reboot or
# a container recreate, so `bash start.sh` inside the container finds everything
# ready. It is ADAPTIVE: it never trusts interface names, and it never requires a
# device to speak first before it is recognised.
#   * Lidar   — physical Ethernet ports come from sysfs (not a name regex); each
#               cabled port is probed ACTIVELY (own the host address, ping the
#               last known lidar address and the subnet broadcast, read the
#               neighbour table), then passively, then a single-cabled-port
#               fallback. Ports with no link are reported as NO CARRIER.
#   * Chassis — identified by DRIVER OWNERSHIP (the gs_usb interface), so it is
#               renamed and configured even while the chassis is silent; traffic
#               is checked afterwards as a diagnostic, not as a gate.
#
#   1. Lidar  — the Ethernet port that carries Livox Mid-360 traffic
#               (ARP "who-has <host-ip>" from the lidar, or Livox UDP broadcast)
#               -> static <LIDAR_HOST_IP>/<CIDR>
#   2. Chassis — the CAN port that carries chassis frames at the target bitrate
#               -> renamed <RANGER_CAN_NAME>, UP
#   3. Camera  — Orbbec by USB vendor id 2bc5 (name-independent), verified only
#
# Detection needs the device powered and online. If a device is off/unplugged,
# the script reports "not detected" rather than guessing a wrong interface.
#
# Idempotent: already-correct interfaces are detected and left as-is.
# Needs root; re-execs under sudo. Override any default via the env vars below.
set -uo pipefail

LIDAR_HOST_IP="${LIDAR_HOST_IP:-192.168.1.50}"
LIDAR_CIDR="${LIDAR_CIDR:-24}"
LIDAR_PROBE_SECS="${LIDAR_PROBE_SECS:-4}"
RANGER_CAN_NAME="${RANGER_CAN_NAME:-can_ranger}"
RANGER_CAN_BITRATE="${RANGER_CAN_BITRATE:-500000}"
CAN_PROBE_SECS="${CAN_PROBE_SECS:-3}"
# JetPack's tegra kernel does NOT compile the gs_usb driver for the candleLight
# USB-CAN adapter (1d50:606f) that the Ranger chassis actually uses, so we load
# an out-of-tree gs_usb.ko built against this kernel. See the CAN handbook:
#   Ranger底盘CAN口驱动问题完整解决手册.md
GS_USB_KO="${GS_USB_KO:-/home/wxf/ranger/gs_usb_build/gs_usb.ko}"
ORBBEC_USB_VID="${ORBBEC_USB_VID:-2bc5}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ROBONIX_ENV_FILE:-$SCRIPT_DIR/.env}"
# Last lidar address we successfully talked to (written back by earlier runs).
# The active probe pings it first, so a lidar whose address is already known is
# found in ~2 s instead of waiting for it to announce itself.
LIDAR_IP="${LIDAR_IP:-$(grep -E '^LIDAR_IP=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)}"

RC=0
ok()   { printf '\033[32m[ OK ]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[FAIL]\033[0m %s\n' "$*"; RC=1; }
info() { printf '  %s\n' "$*"; }

if [[ "$(id -u)" -ne 0 ]]; then
    exec sudo -E bash "$0" "$@"
fi
need() { command -v "$1" >/dev/null 2>&1 || { fail "missing tool: $1 (apt-get install -y $2)"; return 1; }; }
# Like `need`, but self-healing for host tools the probe cannot run without:
# if the binary is absent, apt-get install it (script already re-exec'd as root).
ensure_tool() {   # $1=binary, $2=apt package
    command -v "$1" >/dev/null 2>&1 && return 0
    info "$1 not found — auto-installing (apt-get install -y $2) …"
    if apt-get install -y "$2" >/dev/null 2>&1; then
        ok "installed $1"
        return 0
    fi
    fail "$1 auto-install failed (run: apt-get install -y $2)"
    return 1
}
# Idempotent upsert of KEY=VALUE into $ENV_FILE (create the file if absent).
set_env() {   # $1=key, $2=value
    local key="$1" value="$2"
    if [[ -f "$ENV_FILE" ]] && grep -qE "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}
# The lidar's own address is the sender ("tell <ip>") of the ARP "who-has
# <host-ip>" it keeps broadcasting until we answer. Sniff it directly; fall back
# to the neighbour cache for the already-configured case (lidar knows our MAC and
# stops ARPing). Prints the IP or nothing.
detect_lidar_ip() {   # $1 = nic
    local nic="$1" ip=""
    ip=$(timeout "$((LIDAR_PROBE_SECS+2))" tcpdump -ni "$nic" -c 1 \
        "arp and arp[24:4] = $(printf '0x%02x%02x%02x%02x' ${LIDAR_HOST_IP//./ })" \
        2>/dev/null \
        | grep -oE 'tell [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    if [[ -z "$ip" ]]; then
        ip=$(ip neigh show dev "$nic" 2>/dev/null \
            | awk '$NF != "FAILED" && $1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print $1}' \
            | grep -vx "$LIDAR_HOST_IP" | head -1)
    fi
    printf '%s' "$ip"
}

# ── 1. Lidar NIC (adaptive) ──────────────────────────────────────────────────
# Candidate = every PHYSICAL Ethernet port, decided from sysfs (type==1 ARPHRD_ETHER,
# a /device symlink, no /wireless dir) rather than by a name regex — a regex keeps
# leaking virtual interfaces (`can[0-9]` never matched the renamed `can_ranger`, so
# the CAN port was being probed as if it were an Ethernet port).
#
# Four passes, so a lidar that never announces itself, or that has moved address,
# is still found:
#   1. LIDAR_HOST_IP already live on a port  -> that port carries the lidar
#   2. ACTIVE  — own LIDAR_HOST_IP/CIDR on each cabled port, ping the last known
#                lidar address plus the subnet broadcast, then read the neighbour
#                table. This is what removes the dependency on the lidar speaking
#                first: a lidar answers ARP, it does not announce itself.
#   3. PASSIVE — the lidar's own ARP "who-has <host-ip>" or Livox UDP 5620x.
#   4. FALLBACK— exactly one cabled Ethernet port -> that must be the lidar
# Ports without carrier are reported as NO CARRIER instead of being sniffed in
# silence: "no traffic" and "no cable" are different faults and must read so.
phys_eth_ifaces() {
    local n i
    for n in /sys/class/net/*; do
        i="$(basename "$n")"
        [[ "$(cat "$n/type" 2>/dev/null)" == "1" ]] || continue   # ARPHRD_ETHER
        [[ -e "$n/device" ]] || continue                          # physical only
        [[ -d "$n/wireless" ]] && continue                        # not Wi-Fi
        printf '%s\n' "$i"
    done
}
iface_carrier() { cat "/sys/class/net/$1/carrier" 2>/dev/null || echo 0; }
# Own LIDAR_HOST_IP/CIDR on $1 just long enough to see whether the lidar answers.
# On success prints the lidar's address and LEAVES the address configured; on
# failure removes only the address it added, so a port already configured for
# something else is never disturbed.
lidar_active_probe() {   # $1 = nic
    local nic="$1" answered=""
    ip addr add "$LIDAR_HOST_IP/$LIDAR_CIDR" dev "$nic" 2>/dev/null || return 1
    ip link set "$nic" up 2>/dev/null
    sleep 1                                    # let the address settle
    if [[ -n "$LIDAR_IP" ]] && ping -c 2 -W 1 -I "$nic" "$LIDAR_IP" >/dev/null 2>&1; then
        printf '%s' "$LIDAR_IP"; return 0       # the address we already knew
    fi
    ping -c 2 -W 1 -I "$nic" -b "${LIDAR_HOST_IP%.*}.255" >/dev/null 2>&1 || true
    answered="$(ip neigh show dev "$nic" 2>/dev/null \
        | awk '$NF != "FAILED" && $1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print $1}' \
        | grep -vx "$LIDAR_HOST_IP" | head -1)"
    if [[ -n "$answered" ]]; then
        printf '%s' "$answered"; return 0
    fi
    ip addr del "$LIDAR_HOST_IP/$LIDAR_CIDR" dev "$nic" 2>/dev/null || true
    return 1
}
echo "== lidar NIC (adaptive probe) =="
lidar_nic=""
lidar_ip=""
if ensure_tool tcpdump tcpdump; then
    mapfile -t eth_candidates < <(phys_eth_ifaces)
    # Pass 1 — the host address is already live somewhere.
    already=$(ip -4 -br addr show 2>/dev/null | grep -w "$LIDAR_HOST_IP/$LIDAR_CIDR" | awk '{print $1}' | head -1)
    if [[ -n "$already" ]]; then
        lidar_nic="$already"
        ok "$lidar_nic already carries $LIDAR_HOST_IP/$LIDAR_CIDR"
    else
        cabled=(); dead=()
        for nic in "${eth_candidates[@]}"; do
            ip link set "$nic" up 2>/dev/null
            if [[ "$(iface_carrier "$nic")" == "1" ]]; then
                info "$nic: link UP"
                cabled+=("$nic")
            else
                info "$nic: NO CARRIER — 无链路（网线未插好 / 对端未上电）"
                dead+=("$nic")
            fi
        done

        # Pass 2 — active probe on every port that actually has a link.
        for nic in "${cabled[@]}"; do
            info "active probe on $nic (temporarily owning $LIDAR_HOST_IP) …"
            if lidar_ip="$(lidar_active_probe "$nic")"; then
                lidar_nic="$nic"
                ok "lidar answered on $nic at $lidar_ip (active probe)"
                break
            fi
        done

        # Pass 3 — passive sniff, for a lidar that does announce itself.
        if [[ -z "$lidar_nic" ]]; then
            for nic in "${cabled[@]}"; do
                info "sniffing $nic (${LIDAR_PROBE_SECS}s) …"
                hits=$(timeout "$((LIDAR_PROBE_SECS+2))" tcpdump -ni "$nic" -c 1 \
                        "arp[24:4] = $(printf '0x%02x%02x%02x%02x' ${LIDAR_HOST_IP//./ }) or (udp and (port 56200 or port 56300 or port 56400))" \
                        2>/dev/null | grep -cE 'ARP|UDP|IP' || true)
                if [[ "${hits:-0}" -ge 1 ]]; then
                    lidar_nic="$nic"; break
                fi
            done
        fi

        # Pass 4 — this host has a single RJ45: if it is cabled, it is the lidar.
        if [[ -z "$lidar_nic" && "${#cabled[@]}" -eq 1 ]]; then
            lidar_nic="${cabled[0]}"
            warn "no lidar reply — falling back to the only cabled Ethernet port: $lidar_nic"
        fi

        if [[ -n "$lidar_nic" ]]; then
            ip addr add "$LIDAR_HOST_IP/$LIDAR_CIDR" dev "$lidar_nic" 2>/dev/null || true
            ip link set "$lidar_nic" up 2>/dev/null
            ok "lidar on $lidar_nic → $LIDAR_HOST_IP/$LIDAR_CIDR (UP)"
        elif [[ "${#cabled[@]}" -eq 0 && "${#dead[@]}" -gt 0 ]]; then
            fail "every Ethernet port is NO CARRIER — 网线未插好 / 雷达未上电"
        else
            fail "no Ethernet port carried lidar traffic (lidar powered & cabled?)"
        fi
    fi

    # Read the lidar's own IP and persist it to .env, so the manifest's
    # `lidar_ip: ${LIDAR_IP}` picks it up at boot instead of a hardcoded .3.
    if [[ -n "$lidar_nic" ]]; then
        [[ -n "$lidar_ip" ]] || lidar_ip="$(detect_lidar_ip "$lidar_nic")"
        if [[ -n "$lidar_ip" ]]; then
            set_env LIDAR_IP "$lidar_ip"
            ok "lidar ip → $lidar_ip (persisted to .env as LIDAR_IP)"
        else
            warn "lidar on $lidar_nic but could not read its IP (powered & cabled?)"
        fi
    fi
fi

# ── 2. Chassis CAN (adaptive) ────────────────────────────────────────────────
# The chassis bus is identified by DRIVER OWNERSHIP, not by traffic. This host has
# exactly one USB-CAN adapter, so whichever CAN interface gs_usb owns *is* the
# chassis bus — no frame required. Gating the rename on hearing a frame (the old
# behaviour) meant a powered-down chassis, or one that had only just
# re-enumerated, left the interface named canX, and every consumer of
# `can_ranger` then failed with no recovery but a manual rename.
# Traffic is checked AFTER the interface is in place, as a diagnostic.
echo "== chassis CAN (adaptive probe @ ${RANGER_CAN_BITRATE}bps) =="
# Ensure the gs_usb driver is present before probing — without it the candleLight
# adapter enumerates on USB but no canX interface is ever created. Load from the
# system module tree if available, else fall back to the out-of-tree .ko.
# NB: capture lsmod into a var and match via here-string — a `lsmod | grep -q`
# pipe trips `set -o pipefail` (grep -q closes the pipe, lsmod gets SIGPIPE=141),
# which would wrongly report an already-loaded module as missing.
gs_usb_loaded() { grep -qw gs_usb <<<"$(lsmod 2>/dev/null)"; }
if ! gs_usb_loaded; then
    if modprobe gs_usb 2>/dev/null && gs_usb_loaded; then
        ok "loaded gs_usb (modprobe)"
    elif [[ -f "$GS_USB_KO" ]] && insmod "$GS_USB_KO" 2>/dev/null; then
        ok "loaded gs_usb (insmod $GS_USB_KO)"
    else
        warn "gs_usb not loaded (build it per Ranger底盘CAN口驱动问题完整解决手册.md); CAN probe may find nothing"
    fi
    sleep 1   # give the kernel a moment to create the canX interface
else
    ok "gs_usb already loaded"
fi
can_up_at_bitrate() { ip -details link show "$1" 2>/dev/null | grep -q "state UP" \
                      && ip -details link show "$1" 2>/dev/null | grep -qw "bitrate $RANGER_CAN_BITRATE"; }
can_driver() { basename "$(readlink -f "/sys/class/net/$1/device/driver" 2>/dev/null)" 2>/dev/null; }
can_frames() {   # how many frames $1 carried within CAN_PROBE_SECS
    timeout "$CAN_PROBE_SECS" candump -T "$((CAN_PROBE_SECS*1000))" -n 1 "$1" 2>/dev/null | wc -l
}
configure_can() {   # $1 = CAN iface to (re)configure in place
    ip link set "$1" down 2>/dev/null || return 1
    ip link set "$1" type can bitrate "$RANGER_CAN_BITRATE" 2>/dev/null || return 1
    ip link set "$1" up 2>/dev/null || return 1
    can_up_at_bitrate "$1"
}
if need candump can-utils; then
    if can_up_at_bitrate "$RANGER_CAN_NAME"; then
        ok "$RANGER_CAN_NAME already UP at ${RANGER_CAN_BITRATE}bps"
    else
        gs_ifaces=()
        for c in $(ip -br link show type can 2>/dev/null | awk '{print $1}'); do
            [[ "$(can_driver "$c")" == "gs_usb" ]] && gs_ifaces+=("$c")
        done
        ranger_can=""
        if [[ "${#gs_ifaces[@]}" -eq 0 ]]; then
            fail "no CAN interface is owned by gs_usb — USB-CAN 适配器没插？(lsusb 应见 1d50:606f)"
        elif [[ "${#gs_ifaces[@]}" -eq 1 ]]; then
            ranger_can="${gs_ifaces[0]}"
            info "gs_usb owns $ranger_can — 认定它是底盘总线（不要求它先发帧）"
        else
            # More than one USB-CAN adapter on this host: disambiguate by traffic.
            warn "${#gs_ifaces[@]} 个 gs_usb CAN 口，改按底盘流量区分"
            for c in "${gs_ifaces[@]}"; do
                info "probing $c (${CAN_PROBE_SECS}s) …"
                configure_can "$c" || continue
                if [[ "$(can_frames "$c")" -ge 1 ]]; then
                    ranger_can="$c"; break
                fi
                ip link set "$c" down 2>/dev/null
            done
            [[ -n "$ranger_can" ]] || fail "没有 gs_usb CAN 口收到底盘帧"
        fi

        if [[ -n "$ranger_can" ]]; then
            if [[ "$ranger_can" != "$RANGER_CAN_NAME" ]]; then
                ip link set "$ranger_can" down 2>/dev/null
                if ip link set "$ranger_can" name "$RANGER_CAN_NAME" 2>/dev/null; then
                    ok "$ranger_can → renamed $RANGER_CAN_NAME"
                else
                    fail "could not rename $ranger_can to $RANGER_CAN_NAME"
                fi
            fi
            if ! can_up_at_bitrate "$RANGER_CAN_NAME"; then
                if configure_can "$RANGER_CAN_NAME"; then
                    ok "$RANGER_CAN_NAME UP at ${RANGER_CAN_BITRATE}bps"
                else
                    fail "$RANGER_CAN_NAME did not come UP at ${RANGER_CAN_BITRATE}bps"
                fi
            fi
            # Traffic is a DIAGNOSTIC, not a gate: the interface is correctly named
            # and configured either way. A silent bus means the CHASSIS needs
            # attention, so the message has to say which side to look at.
            if [[ "$(can_frames "$RANGER_CAN_NAME")" -eq 0 ]]; then
                warn "$RANGER_CAN_NAME 已就位，但 ${CAN_PROBE_SECS}s 内没收到底盘帧"
                info "适配器 / USB / 驱动都正常 —— 要查的是底盘侧："
                info "  · 底盘是否上电（电源开关、急停是否松开）"
                info "  · CAN 线是否接好（适配器端子 + 底盘侧接头）"
            else
                ok "chassis frames present on $RANGER_CAN_NAME"
            fi
        fi
    fi
fi

# ── 2b. Chassis out of STANDBY, into CAN control mode ────────────────────────
# The chassis boots in CONTROL_MODE_STANDBY (0x00) and ignores every motion
# command until it is switched to CONTROL_MODE_CAN (0x01). ranger_base_node does
# send that switch once at startup (AgilexBase::EnableCommandedMode → CAN id
# 0x421, data 01 00 …), but a single lost frame leaves the car dead-silent with a
# perfectly healthy-looking bus, so assert the mode here on every run.
#
# The frame is lost when the gs_usb TX path has wedged: the driver leaks echo
# slots ("Unexpected unused echo id N" in dmesg), stops the queue for good, and
# every write() then returns ENOBUFS while RX keeps working and the TX packet
# counter freezes. Only a link down/up releases the slots, so probe TX first and
# reset the link ONLY when it is actually wedged — the reset drops the socket a
# running ranger_base_node holds, which then needs restarting.
#
# Wire format (ugv_sdk protocol v2):
#   TX 0x421 dlc8 `01 00 00 00 00 00 00 00`  = set control mode CAN
#   RX 0x211 dlc8 byte0=vehicle_state byte1=control_mode (00 STANDBY / 01 CAN)
echo "== chassis control mode (never STANDBY) =="
CAN_MODE_FRAME="421#0100000000000000"
chassis_control_mode() {   # echoes byte1 of 0x211, empty on timeout
    candump -T 2000 -n 1 "$RANGER_CAN_NAME,211:7FF" 2>/dev/null | awk '{print $5}'
}
if can_up_at_bitrate "$RANGER_CAN_NAME" && need cansend can-utils; then
    # 1) TX health probe — one frame; ENOBUFS means the gs_usb queue is wedged.
    if ! cansend "$RANGER_CAN_NAME" "$CAN_MODE_FRAME" 2>/dev/null; then
        warn "$RANGER_CAN_NAME TX wedged (ENOBUFS, gs_usb echo-id leak) — resetting link"
        ip link set "$RANGER_CAN_NAME" down 2>/dev/null || true
        sleep 1
        ip link set "$RANGER_CAN_NAME" type can bitrate "$RANGER_CAN_BITRATE" 2>/dev/null || true
        ip link set "$RANGER_CAN_NAME" up 2>/dev/null || true
        sleep 1
        if can_up_at_bitrate "$RANGER_CAN_NAME"; then
            ok "$RANGER_CAN_NAME reset, TX queue released"
            warn "restart the chassis node — its CAN socket died with the link"
        else
            fail "$RANGER_CAN_NAME did not come back UP after reset"
        fi
    fi
    # 2) Assert CAN mode, verify against the chassis heartbeat.
    # A silent bus is a CHASSIS-side condition, not an interface one: the link is
    # already named and configured correctly by this point, so report it and move
    # on rather than failing the whole bring-up — and skip the pointless retries.
    if [[ "$(can_frames "$RANGER_CAN_NAME")" -eq 0 ]]; then
        warn "$RANGER_CAN_NAME 上没有底盘帧 —— 跳过控制模式断言"
        info "接口配置本身正常；要查的是底盘侧：上电 / CAN 线 / 急停"
    else
        can_mode=""
        for attempt in 1 2 3 4 5; do
            cansend "$RANGER_CAN_NAME" "$CAN_MODE_FRAME" 2>/dev/null || true
            sleep 0.3
            can_mode=$(chassis_control_mode)
            [[ "$can_mode" == "01" ]] && break
            info "control mode still '${can_mode:-no 0x211 frame}' (attempt $attempt/5) …"
        done
        case "$can_mode" in
            01) ok "chassis in CAN control mode (0x211 byte1=01, not STANDBY)" ;;
            00) warn "chassis still in STANDBY — 松开急停、关掉遥控器后重跑" ;;
            "") warn "chassis 有帧但读不到 0x211 心跳（底盘固件状态异常？）" ;;
            *)  warn "chassis control mode is 0x$can_mode (00=STANDBY 01=CAN 02=UART 03=RC)" ;;
        esac
    fi
fi

# ── 3. Camera (verify only, by USB vendor id) ─────────────────────────────────
# Orbbec is a USB char device; the container's --device-cgroup-rule 'c 189:* rmw'
# grants open() access. Match by vendor id so any Orbbec model / bus / port works.
echo "== camera (Orbbec USB vendor $ORBBEC_USB_VID) =="
if need lsusb usbutils; then
    line=$(lsusb 2>/dev/null | grep -iE "ID ${ORBBEC_USB_VID}:" | head -1)
    if [[ -n "$line" ]]; then
        ok "Orbbec present: ${line#*ID }"
    else
        fail "no Orbbec (vendor $ORBBEC_USB_VID) on USB (camera plugged in?)"
    fi
fi

# ── 4. Docker CDI (NVIDIA GPU access) ─────────────────────────────────────────
# Container Device Interface config allows docker to expose GPUs to containers.
# Jetson 重启后设备节点路径可能变，每次都重新生成 CDI 确保可靠。
echo "== Docker CDI (NVIDIA GPU) =="
if need nvidia-ctk nvidia-container-toolkit; then
    # 检查关键 GPU 设备节点
    missing_devs=()
    for dev in /dev/nvhost-ctrl-gpu /dev/nvhost-as-gpu /dev/nvhost-gpu /dev/nvmap; do
        [[ -e "$dev" ]] || missing_devs+=("$dev")
    done

    if [[ ${#missing_devs[@]} -gt 0 ]]; then
        info "GPU 设备节点未就绪: ${missing_devs[*]}"
        info "运行 jetson_clocks 触发 GPU 完全初始化..."
        if jetson_clocks >/dev/null 2>&1; then
            sleep 2  # 等待设备节点出现
            # 重新检查
            still_missing=()
            for dev in /dev/nvhost-ctrl-gpu /dev/nvhost-as-gpu /dev/nvhost-gpu; do
                [[ -e "$dev" ]] || still_missing+=("$dev")
            done
            if [[ ${#still_missing[@]} -gt 0 ]]; then
                fail "GPU 设备节点仍缺失: ${still_missing[*]} — 需要重启 Jetson"
            else
                ok "GPU 设备节点已初始化"
            fi
        else
            fail "jetson_clocks 执行失败（需要 sudo？）"
        fi
    else
        ok "GPU 设备节点就绪"
    fi

    info "重新生成 CDI（幂等，确保设备节点最新）..."
    if nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml >/dev/null 2>&1; then
        cdi_count=$(nvidia-ctk cdi list 2>&1 | grep -oP 'Found \K[0-9]+' || echo 0)
        if [[ "${cdi_count:-0}" -ge 1 ]]; then
            ok "CDI 已更新 → $cdi_count device(s)，容器可启动"
        else
            fail "CDI 生成成功但无设备列出（驱动异常？）"
        fi
    else
        fail "nvidia-ctk cdi generate 失败（NVIDIA 驱动未安装？）"
    fi
fi

echo
[[ $RC -eq 0 ]] && ok "all hardware interfaces configured and ready" \
                || fail "one or more interfaces need attention (see above)"
exit $RC
