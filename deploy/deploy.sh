#!/usr/bin/env bash
# ============================================================
# 轻痕博客 · 一键部署脚本
# 本地构建 + rsync 增量上传到 512MB Linux 服务器
#
# 首次使用前：
#   1. 修改下面的 SERVER / DEST 变量
#   2. 服务器上执行一次初始化（见 README「服务器初始化」）
# ============================================================
set -euo pipefail

# ---------- 按需修改这两行 ----------
SERVER="user@your-server-ip"          # SSH 目标
DEST="/var/www/blog"                  # 服务器上的站点目录
# ------------------------------------

echo "==> 构建静态站点..."
npm run build

echo "==> 上传到 ${SERVER}:${DEST} ..."
# --delete 保证远端与本地完全一致；-z 传输时压缩
rsync -avz --delete dist/ "${SERVER}:${DEST}/"

echo "==> 完成！站点已更新。"
ssh "${SERVER}" "nginx -s reload" || echo "（提示：nginx reload 可在服务器上手动执行）"
