#!/bin/sh
# install-board.sh — I.MX6ULL 开发板「断电自恢复」一键安装（自包含版）
#
# 目标平台：正点原子 I.MX6ULL（Yocto + sysvinit，无 systemd）
# 装什么：固定 MAC → 静态 IP → frpc 开机自启 → cron 进程守护 → 校时
#
# 用法：
#   scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
#       deploy/install-board.sh root@192.168.137.91:/tmp/
#   ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
#       root@192.168.137.91 'sh /tmp/install-board.sh'
#
#   sh install-board.sh --no-ip      # 只装自启和守护，不动 IP
#   sh install-board.sh --dry-run    # 只打印将要做什么，不落盘
#
# 幂等：可以重复执行，不会重复加 cron、不会覆盖已正确的配置。
#
# 这个文件是自包含的：三个脚本（set-mac / frpc / keepalive）以 heredoc 内嵌，
# 不需要另外 scp 任何文件。

export PATH=/sbin:/usr/sbin:/usr/bin:/bin

# ======== 按你的板子改这几行 ========
MAC_ETH0=88:60:44:c2:46:16
MAC_ETH1=aa:60:44:c2:46:16
STATIC_IP=192.168.137.91
NETMASK=255.255.255.0
GATEWAY=192.168.137.1
NTP_SERVERS="ntp.aliyun.com ntp1.aliyun.com"
FRPS_ADDR=112.124.58.134
# ===================================

DO_IP=1
DRY=0
for a in "$@"; do
    case "$a" in
        --no-ip)   DO_IP=0 ;;
        --dry-run) DRY=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    esac
done

log()  { printf '==> %s\n' "$*"; }
ok()   { printf '    [OK] %s\n' "$*"; }
warn() { printf '    [!!] %s\n' "$*"; }
run()  { if [ "$DRY" = "1" ]; then printf '    (dry-run) %s\n' "$*"; else eval "$@"; fi; }

[ "$(id -u)" = "0" ] || { echo "必须用 root 运行"; exit 1; }

# ---------------- 0. 环境自检 ----------------
log "0/6 环境自检"
if [ -d /run/systemd/system ]; then
    warn "这台机器有 systemd，本脚本是给 sysvinit 板子用的，请改用 systemctl"
    exit 1
fi
ok "PID1 = $(readlink -f /proc/1/exe)"

if : > /etc/.rwtest 2>/dev/null; then
    rm -f /etc/.rwtest
    ok "根文件系统可写，改动会持久保存"
else
    warn "根文件系统是只读的，下面的改动重启后会丢"
fi

_conn=$(ls /etc/rc5.d/ 2>/dev/null | grep -i connman | sed 's/^S//' | cut -c1-2) || true
[ -n "$_conn" ] && ok "connman 启动序号 S$_conn（set-mac 会用 S04，必须更小）"

# ---------------- 1. 固定 MAC ----------------
log "1/6 固定 MAC（必须早于 connman 启动）"

if [ "$DRY" = "0" ]; then
cat > /etc/init.d/set-mac <<'INIT_EOF'
#!/bin/sh
### BEGIN INIT INFO
# Provides:          set-mac
# Default-Start:     5
# Short-Description: 固定 eth0 / eth1 的 MAC 地址
### END INIT INFO
#
# 为什么需要：设备树 local-mac-address 全 0，内核 fec 驱动会 eth_hw_addr_random()
# 随机生成 MAC；而 connman 的服务名是 ethernet_<MAC>_cable，MAC 一变服务名就变，
# 之前在旧服务名下配的静态 IP 就永远匹配不上，只能退回 DHCP。
# 所以必须在 connman（rc5.d/S05connman）之前把 MAC 钉死。

IFCONFIG=/sbin/ifconfig

set_mac() {
    iface=$1
    mac=$2
    [ -e "/sys/class/net/$iface" ] || return 0
    cur=$(cat "/sys/class/net/$iface/address" 2>/dev/null)
    [ "$cur" = "$mac" ] && return 0
    echo "set-mac: $iface $cur -> $mac"
    $IFCONFIG "$iface" down 2>/dev/null
    $IFCONFIG "$iface" hw ether "$mac" 2>/dev/null
    $IFCONFIG "$iface" up 2>/dev/null
    return 0
}

case "$1" in
    start|"")
        set_mac eth0 @MAC_ETH0@
        set_mac eth1 @MAC_ETH1@
        ;;
    stop|restart|reload|force-reload)
        ;;
    *)
        echo "Usage: $0 {start}"; exit 1 ;;
esac

exit 0
INIT_EOF
sed -i "s|@MAC_ETH0@|$MAC_ETH0|g; s|@MAC_ETH1@|$MAC_ETH1|g" /etc/init.d/set-mac
chmod 755 /etc/init.d/set-mac
fi
run "ln -sf ../init.d/set-mac /etc/rc5.d/S04set-mac"
ok "/etc/init.d/set-mac -> rc5.d/S04set-mac"

if [ -n "$_conn" ] && [ "$_conn" -le 4 ] 2>/dev/null; then
    warn "connman 序号 S$_conn 不大于 4，请把 S04set-mac 改成更小的序号"
fi

CUR_MAC=$(cat /sys/class/net/eth0/address)
if [ "$CUR_MAC" = "$MAC_ETH0" ]; then
    ok "eth0 MAC 已是 $CUR_MAC，无需改动"
elif [ "$DRY" = "1" ]; then
    printf '    (dry-run) ifconfig eth0 hw ether %s\n' "$MAC_ETH0"
else
    # 注意：SSH 连在 eth0 上时这一步会短暂断链，脚本会把网口再 up 起来
    ifconfig eth0 down 2>/dev/null || true
    ifconfig eth0 hw ether "$MAC_ETH0" 2>/dev/null || true
    ifconfig eth0 up 2>/dev/null || true
    ok "eth0 MAC $CUR_MAC -> $(cat /sys/class/net/eth0/address)"
fi

# ---------------- 2. 静态 IP ----------------
if [ "$DO_IP" = "1" ]; then
    log "2/6 静态 IP $STATIC_IP（ICS 网段写死，DHCP 不支持 MAC 绑定）"
    SVC=$(connmanctl services 2>/dev/null | grep -o 'ethernet_[0-9a-f]*_cable' | head -1) || true
    if [ -z "$SVC" ]; then
        warn "没找到 connman 的 ethernet 服务，跳过（先确认网线插好）"
    elif grep -q 'IPv4.method=manual' "/var/lib/connman/$SVC/settings" 2>/dev/null; then
        ok "$SVC 已经是 manual，跳过"
    else
        run "connmanctl config $SVC --ipv4 manual $STATIC_IP $NETMASK $GATEWAY"
        run "connmanctl config $SVC --nameservers $GATEWAY"
        ok "已设为 $STATIC_IP/24，网关 $GATEWAY"
    fi
    # 清掉随机 MAC 留下的废弃服务
    for d in /var/lib/connman/ethernet_*; do
        [ -d "$d" ] || continue
        [ "$(basename "$d")" = "$SVC" ] && continue
        run "rm -rf $d"
        ok "清理废弃服务 $(basename "$d")"
    done
else
    log "2/6 跳过静态 IP（--no-ip）"
fi

# ---------------- 3. frpc 开机自启 ----------------
log "3/6 frpc 开机自启（rc5.d 里 nginx 是 S92，frpc 用 S99 排在后面）"

if [ "$DRY" = "0" ]; then
cat > /etc/init.d/frpc <<'INIT_EOF'
#!/bin/sh
### BEGIN INIT INFO
# Provides:          frpc
# Required-Start:    $network $remote_fs
# Required-Stop:     $network $remote_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: frp client — 把板上 80 端口的静态站经反向隧道发布到公网
### END INIT INFO
#
# 日志放 /var/lib/frpc/（rootfs，持久），不放 /var/log
# —— Yocto 的 /var/log 指向 /var/volatile/log，tmpfs，断电即丢。

DAEMON=/usr/local/bin/frpc
CONF=/etc/frp/frpc.toml
PIDFILE=/var/run/frpc.pid
LOGDIR=/var/lib/frpc
LOG=$LOGDIR/frpc.log
MAXLOG=2097152
SERVER=@FRPS_ADDR@

is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

rotate() {
    mkdir -p "$LOGDIR"
    [ -f "$LOG" ] || return 0
    sz=$(wc -c < "$LOG" 2>/dev/null || echo 0)
    if [ "$sz" -gt "$MAXLOG" ]; then
        tail -c 262144 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
}

# 开机时 connman 往往还没拿到地址，这里最多等 30s；
# 配合 frpc.toml 的 loginFailExit=false，避免登录失败一次就退出
wait_net() {
    i=0
    while [ $i -lt 30 ]; do
        ping -c1 -W1 "$SERVER" >/dev/null 2>&1 && return 0
        sleep 1
        i=$((i + 1))
    done
    return 1
}

case "$1" in
    start)
        if is_running; then echo "frpc already running (pid $(cat "$PIDFILE"))"; exit 0; fi
        rotate
        echo -n "Starting frpc: "
        wait_net
        "$DAEMON" -c "$CONF" >>"$LOG" 2>&1 &
        echo $! > "$PIDFILE"
        sleep 2
        if is_running; then
            echo "ok (pid $(cat "$PIDFILE"))"
        else
            echo "failed, 末尾日志："; tail -n 5 "$LOG" 2>/dev/null
            rm -f "$PIDFILE"; exit 1
        fi
        ;;
    stop)
        if ! is_running; then echo "frpc not running"; rm -f "$PIDFILE"; exit 0; fi
        echo -n "Stopping frpc: "
        kill "$(cat "$PIDFILE")" 2>/dev/null
        i=0
        while is_running && [ $i -lt 5 ]; do sleep 1; i=$((i + 1)); done
        is_running && kill -9 "$(cat "$PIDFILE")" 2>/dev/null
        rm -f "$PIDFILE"
        echo "ok"
        ;;
    restart) "$0" stop; sleep 1; "$0" start ;;
    status)
        if is_running; then echo "frpc running (pid $(cat "$PIDFILE"))"
        else echo "frpc stopped"; exit 3; fi
        ;;
    *) echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac

exit 0
INIT_EOF
sed -i "s|@FRPS_ADDR@|$FRPS_ADDR|g" /etc/init.d/frpc
chmod 755 /etc/init.d/frpc
fi

run "ln -sf /etc/init.d/frpc /etc/rc5.d/S99frpc"
run "ln -sf /etc/init.d/frpc /etc/rc0.d/K01frpc"
run "ln -sf /etc/init.d/frpc /etc/rc6.d/K01frpc"
run "mkdir -p /var/lib/frpc"
ok "S99frpc / K01frpc 就位，日志目录 /var/lib/frpc"

if [ -f /etc/frp/frpc.toml ]; then
    if /etc/init.d/frpc status >/dev/null 2>&1; then
        ok "frpc 已在运行（pid $(cat /var/run/frpc.pid 2>/dev/null)）"
    else
        run "/etc/init.d/frpc start >/dev/null 2>&1 || true"
        ok "frpc 状态：$(/etc/init.d/frpc status 2>&1)"
    fi
else
    warn "/etc/frp/frpc.toml 不存在，先配好 frp"
fi

# ---------------- 4. cron 守护 ----------------
log "4/6 cron 守护（sysvinit 没有 Restart=always，靠每分钟巡检补）"

if [ "$DRY" = "0" ]; then
cat > /usr/local/bin/board-keepalive.sh <<'KA_EOF'
#!/bin/sh
# board-keepalive.sh · sysvinit 上的"穷人版 Restart=always"
# sysvinit 只会把服务拉起来一次；进程挂了（OOM、误 kill）没人管。
# 用 cron 每分钟巡检一次，兼做开机首次校时。
# 日志写 /var/lib/frpc/（rootfs，持久），不写 /var/log（tmpfs，断电丢）

LOGDIR=/var/lib/frpc
LOG=$LOGDIR/keepalive.log
LOCK=/var/run/keepalive.lock
MAXLOG=1048576

mkdir -p "$LOGDIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
    sz=$(wc -c < "$LOG" 2>/dev/null || echo 0)
    if [ "$sz" -gt "$MAXLOG" ]; then
        tail -c 131072 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
}

# 上一轮没跑完就跳过，避免 cron 叠加
if ! mkdir "$LOCK" 2>/dev/null; then exit 0; fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# 开机后第一次运行时校时。/var/run 是 tmpfs，重启即清空，正好当标记。
if [ ! -f /var/run/.time_synced ]; then
    if /usr/bin/ntpdate-sync silent >/dev/null 2>&1; then
        touch /var/run/.time_synced
        log "开机校时完成：$(date '+%Y-%m-%d %H:%M:%S')"
    fi
fi

if ! pidof nginx >/dev/null 2>&1; then
    log "nginx 不在运行，重启"
    /etc/init.d/nginx start >>"$LOG" 2>&1
    sleep 1
    if pidof nginx >/dev/null 2>&1; then log "nginx 已恢复"; else log "nginx 重启失败"; fi
fi

if ! /etc/init.d/frpc status >/dev/null 2>&1; then
    log "frpc 不在运行，重启"
    /etc/init.d/frpc start >>"$LOG" 2>&1
    sleep 1
    if /etc/init.d/frpc status >/dev/null 2>&1; then log "frpc 已恢复"; else log "frpc 重启失败"; fi
fi

exit 0
KA_EOF
chmod 755 /usr/local/bin/board-keepalive.sh
fi

if crontab -l 2>/dev/null | grep -q 'board-keepalive.sh'; then
    ok "crontab 已有条目，跳过"
elif [ "$DRY" = "1" ]; then
    printf '    (dry-run) crontab 追加：* * * * * /usr/local/bin/board-keepalive.sh\n'
else
    (crontab -l 2>/dev/null; echo '* * * * * /usr/local/bin/board-keepalive.sh') | crontab -
    ok "已加入 crontab"
fi
if ps | grep -q '[c]rond'; then ok "crond 在跑"; else warn "crond 没在跑：/etc/init.d/crond start"; fi

# ---------------- 5. 校时 ----------------
log "5/6 校时（板上 NTPSERVERS 默认是空的，ntpdate-sync 会直接 exit 0）"
if grep -q '^NTPSERVERS=""' /etc/default/ntpdate 2>/dev/null; then
    run "sed -i 's|^NTPSERVERS=.*|NTPSERVERS=\"$NTP_SERVERS\"|' /etc/default/ntpdate"
    run "sed -i 's|^UPDATE_HWCLOCK=.*|UPDATE_HWCLOCK=\"yes\"|' /etc/default/ntpdate"
    ok "已填 NTPSERVERS=$NTP_SERVERS，并开启写 RTC"
else
    ok "NTPSERVERS 已配置，跳过"
fi
if ntpdate-sync silent >/dev/null 2>&1; then ok "校时成功：$(date)"; else warn "校时失败（可能还没联网）"; fi

# ---------------- 6. 汇总 ----------------
log "6/6 结果"
echo "    MAC    : $(cat /sys/class/net/eth0/address)"
echo "    IP     : $(ifconfig eth0 2>/dev/null | sed -n 's/.*inet addr:\([0-9.]*\).*/\1/p')"
echo "    nginx  : $(pidof nginx | wc -w) 个进程"
echo "    frpc   : $(/etc/init.d/frpc status 2>&1)"
echo "    自启链 : $(ls /etc/rc5.d/ 2>/dev/null | grep -E 'set-mac|nginx|frpc' | tr '\n' ' ')"
echo ""
log "验证：sync && /sbin/reboot，重启两次确认 MAC 和 IP 都不变"
log "安全网：串口 115200，inittab 里 getty -l /bin/autologin，免密 root"
