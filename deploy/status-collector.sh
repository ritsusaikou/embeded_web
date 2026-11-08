#!/bin/sh
# ============================================================
# 开发板侧：把运行状态写成 status.json
# 部署：/usr/local/bin/status-collector.sh  chmod +x
# 触发：systemd timer，每 10 秒一次（见 status-collector.timer）
#
# 为什么不常驻一个接口进程：
#   为了几行状态拉起一个 Node/Python 服务要 40~120MB 内存，
#   而状态数据的时效要求只是「十秒级」，定时生成完全够用。
# ============================================================

OUT="${OUT:-/var/www/site/status.json}"
TMP="$OUT.new"

# ---- uptime ----
UP=$(cut -d. -f1 /proc/uptime)
D=$((UP / 86400))
H=$(((UP % 86400) / 3600))
M=$(((UP % 3600) / 60))

# ---- 内存（MB）----
MEM_TOTAL=$(awk '/MemTotal/     {printf "%d", $2/1024}' /proc/meminfo)
MEM_AVAIL=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)
MEM_USED=$((MEM_TOTAL - MEM_AVAIL))

# ---- Nginx ----
NGX_VER=$(nginx -v 2>&1 | sed 's#.*/##')
NGX_RSS=$(ps -o rss= -C nginx 2>/dev/null | awk '{s+=$1} END {printf "%.1f", s/1024}')

# ---- frp 隧道 ----
TUNNEL="frp · 未连接"
if pgrep -x frpc >/dev/null 2>&1; then
    FRP_VER=$(frpc -v 2>/dev/null | awk '{print $3}')
    TUNNEL="frp ${FRP_VER:-?} · 已连通"
fi

cat > "$TMP" <<JSON
{
  "host": "$(hostname)",
  "kernel": "$(uname -r)",
  "uptime": "${D}d ${H}h ${M}m",
  "mem": "${MEM_USED} / ${MEM_TOTAL} MB",
  "nginx": "${NGX_VER} · worker rss ${NGX_RSS}MB",
  "tunnel": "${TUNNEL}",
  "tls": "TLS 1.3 · 公网终结",
  "collectedAt": "$(date '+%Y-%m-%d %H:%M')"
}
JSON

# 原子替换：避免前端读到只写了一半的文件
mv "$TMP" "$OUT"
