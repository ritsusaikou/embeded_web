#!/bin/sh
# board-time.sh · 用 HTTP 响应头校时
#
# 为什么不用 ntpdate：
#   板上 /usr/bin/ntpdate-sync 的正文第一行是
#       test -x /usr/sbin/ntpdate || exit 0
#   而 Yocto 镜像里根本没有 /usr/sbin/ntpdate，所以它每次都静默退出，
#   从开机到现在一次都没校成功过（表现就是日志时间停在几个月前）。
#
#   而且 NTP 走 UDP/123，很多校园网/公司网会挡掉；HTTP 一定通。
#
# 原理：任何 HTTP 服务器都会在响应头带 Date，那是服务器时间（UTC），
# 精度 1 秒，对本项目足够（日志时间戳、证书有效期判断都用得上）。
#
# 用法：
#   board-time.sh            # 本次开机还没校过才校（靠 /var/run 的标记，重启即清）
#   board-time.sh --force    # 强制校一次

MARK=/var/run/.time_synced

if [ "$1" != "--force" ] && [ -f "$MARK" ]; then
    exit 0
fi

for url in http://www.baidu.com http://www.qq.com http://connect.rom.miui.com/generate_204; do
    d=$(curl -sI --max-time 6 "$url" 2>/dev/null | sed -n 's/^[Dd]ate: //p' | tr -d '\r' | head -1)
    [ -n "$d" ] || continue

    if python3 -c '
import sys, email.utils, calendar, time, os
try:
    ts = calendar.timegm(email.utils.parsedate(sys.argv[1]))
except Exception:
    sys.exit(1)
# reject obviously bogus values (parse failure yields tiny numbers)
if ts < 1600000000:
    sys.exit(1)
g = time.gmtime(ts)
os.system("date -u -s \"%04d-%02d-%02d %02d:%02d:%02d\"" % (g[:6]))
' "$d" 2>/dev/null; then
        touch "$MARK"
        echo "time synced: $(date -u)"
        exit 0
    fi
done

echo "time sync failed (no reachable http date)"
exit 1
