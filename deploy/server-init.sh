#!/usr/bin/env bash
# ============================================================
# 轻痕博客 · 服务器初始化（在 512MB Linux 服务器上执行一次）
# 适用：Debian / Ubuntu 系
# ============================================================
set -euo pipefail

SITE_DIR="/var/www/blog"
NGINX_CONF="blog.conf"

echo "==> 安装 Nginx ..."
apt update && apt install -y nginx

echo "==> 创建站点目录 ..."
mkdir -p "${SITE_DIR}"
# 归属设为 www-data，普通用户通过 rsync 上传时也能写入（按需调整）
chown -R www-data:www-data "${SITE_DIR}"

echo "==> 安装站点配置 ..."
cp nginx.conf "/etc/nginx/sites-available/${NGINX_CONF}"
ln -sf "/etc/nginx/sites-available/${NGINX_CONF}" "/etc/nginx/sites-enabled/${NGINX_CONF}"
# 小内存机上可移除默认站点，省一个 server 块
rm -f /etc/nginx/sites-enabled/default

echo "==> 校验并重载 ..."
nginx -t && systemctl reload nginx

echo "==> 完成！接下来在本机执行 deploy.sh 推送内容即可。"
echo "    建议：sudo apt install certbot python3-certbot-nginx && sudo certbot --nginx -d <你的域名>"
