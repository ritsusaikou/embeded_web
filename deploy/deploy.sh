#!/usr/bin/env bash
# ============================================================
# 构建 + 发布到 i.MX6ULL 开发板
#
# 用法：
#   BOARD=root@192.168.1.50 npm run deploy
#   （不传环境变量则使用下面的默认值）
#
# 前提：PC 与板子同网段，且已配置 SSH 免密登录
# ============================================================
set -euo pipefail

BOARD="${BOARD:-root@192.168.1.50}"
DEST="${DEST:-/var/www/site}"

echo "==> [1/4] 构建静态站点"
npm run build

echo "==> [2/4] 预压缩（板上 Nginx 用 gzip_static 直接发 .gz）"
find dist -type f \
  \( -name '*.html' -o -name '*.css' -o -name '*.xml' -o -name '*.json' -o -name '*.svg' \) \
  -exec gzip -9 -k -f {} \;

echo "==> [3/4] 同步到 ${BOARD}:${DEST}"
rsync -avz --delete dist/ "${BOARD}:${DEST}/"

echo "==> [4/4] 重载板上 Nginx"
ssh "${BOARD}" "nginx -t && nginx -s reload"

echo "==> 完成：站点已更新"
