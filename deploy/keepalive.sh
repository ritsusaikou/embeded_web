#!/bin/sh
# board-keepalive.sh · sysvinit 上的"穷人版 Restart=always"
#
# sysvinit 只会把服务拉起来一次，进程挂了（OOM、踩内存、误 kill）没人管。
# systemd 靠 Restart=always 兜底，这块板子没有 systemd，所以用 cron 每分钟巡检一次。
#
# 安装：
#   install -m 755 deploy/keepalive.sh /usr/local/bin/board-keepalive.sh
#   crontab: * * * * * /usr/local/bin/board-keepalive.sh
#
# 日志同样写 /var/lib/frpc/（rootfs，持久），不写 /var/log（tmpfs，断电丢）

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

# 上一轮还没跑完就跳过，避免 cron 叠加
if ! mkdir "$LOCK" 2>/dev/null; then
    exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# 开机后第一次运行时校一次时间。
# 板上 RTC 断电后时间会错（这块板实测慢了 7 天），而 rc.local 执行时网络多半还没就绪，
# 所以不在 rc.local 里同步，改在这里——cron 启动时网络已经通了。
# /var/run 是 tmpfs，重启即清空，正好当"本次开机是否已校时"的标记。
if [ ! -f /var/run/.time_synced ]; then
    if /usr/local/bin/board-time.sh >/dev/null 2>&1; then
        log "开机校时完成：$(date '+%Y-%m-%d %H:%M:%S')"
    else
        log "开机校时失败，下一轮再试"
    fi
fi

# Nginx
if ! pidof nginx >/dev/null 2>&1; then
    log "nginx 不在运行，重启"
    /etc/init.d/nginx start >>"$LOG" 2>&1
    sleep 1
    if pidof nginx >/dev/null 2>&1; then
        log "nginx 已恢复"
    else
        log "nginx 重启失败"
    fi
fi

# frpc
if ! /etc/init.d/frpc status >/dev/null 2>&1; then
    log "frpc 不在运行，重启"
    /etc/init.d/frpc start >>"$LOG" 2>&1
    sleep 1
    if /etc/init.d/frpc status >/dev/null 2>&1; then
        log "frpc 已恢复"
    else
        log "frpc 重启失败"
    fi
fi

# board-agent（第 17 步装的；只看进程，不看 /agent/ 反代是否通）
if [ -x /etc/init.d/board-agent ]; then
    if ! /etc/init.d/board-agent status >/dev/null 2>&1; then
        log "board-agent 不在运行，重启"
        /etc/init.d/board-agent start >>"$LOG" 2>&1
        sleep 1
        if /etc/init.d/board-agent status >/dev/null 2>&1; then
            log "board-agent 已恢复"
        else
            log "board-agent 重启失败"
        fi
    fi
fi

exit 0
