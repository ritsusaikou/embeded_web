#!/usr/bin/env bash
# ============================================================
# 公网服务器初始化（只做一次）
#   - 安装 Nginx 与 frp
#   - 写入 443 站点配置，反代到 frps 映射端口
#   - 申请 Let's Encrypt 证书
#
# 用法：
#   sudo DOMAIN=imx6ull.example.com EMAIL=you@example.com bash server-init.sh
# ============================================================
set -euo pipefail

DOMAIN="${DOMAIN:?请设置 DOMAIN，例如 imx6ull.example.com}"
EMAIL="${EMAIL:?请设置 EMAIL，用于证书通知}"
UPSTREAM="127.0.0.1:8080"     # 与 frpc 的 remotePort 一致

echo "==> [1/4] 安装依赖"
apt update
apt install -y nginx certbot python3-certbot-nginx
# frp 请从 release 下载对应架构的压缩包，解压后：
#   install -m 755 frps frpc /usr/local/bin/

echo "==> [2/4] 写入站点配置（先 80 端口，供 ACME 校验）"
cat > /etc/nginx/sites-available/board-site <<CONF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://${UPSTREAM};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
CONF

ln -sf /etc/nginx/sites-available/board-site /etc/nginx/sites-enabled/board-site
nginx -t
systemctl reload nginx

echo "==> [3/4] 申请证书（自动改写配置为 443 + 80 跳转）"
certbot --nginx -d "${DOMAIN}" -m "${EMAIL}" --agree-tos --redirect -n

echo "==> [4/4] 完成"
echo "    下一步：在本机配置 frps.toml 与 frpc.toml，然后："
echo "    systemctl enable --now frps        # 服务器"
echo "    systemctl enable --now frpc        # 开发板"
echo "    验证：curl -I https://${DOMAIN}"
