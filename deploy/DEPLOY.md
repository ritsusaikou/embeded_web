# 部署文档 · i.MX6ULL 静态站点 + frp 反向隧道

站点跑在 i.MX6ULL 开发板上，经 frp 反向隧道发布到 `https://board.nexuslink.icu`。
本文记录**两种 Web 服务方案**的实现与原理，以及服务器侧的公共部分。

**环境实测值**

| 角色 | 配置 |
| --- | --- |
| 开发板 | 正点原子 ATK-IMX6U，i.MX6ULL / 单核 Cortex-A7，494MB 内存，kernel 4.1.15，Yocto + sysvinit，nginx 1.8.1 |
| 服务器 | 阿里云 ECS，Ubuntu 24.04，nginx 1.24.0，2GB 内存 |
| 站点 | Astro 5 静态输出，23 页 / 29 文件 / 556KB |
| 隧道 | frp 0.58.1（frps x86_64 / frpc arm） |

---

## 1. 两种方案

板上"由谁把文件发出去"有两种选择，其余部分完全相同。

| | 方案 A · 板上 nginx | 方案 B · frpc 静态插件 |
| --- | --- | --- |
| Web 服务器 | nginx 1.8.1（板上已装） | frpc 内置 `static_file` 插件 |
| frpc 配置 | `type=tcp` + `localIP/localPort=127.0.0.1:80` | `[proxies.plugin] type="static_file"` |
| 进程数 | nginx master + worker，frpc | 只有 frpc |
| 内存占用 | nginx 5.5MB + frpc 约 6MB | 只有 frpc 约 6MB |
| 压缩 | `gzip_static` 发预压缩 `.gz`，首页 27192B → **6739B** | 无压缩，27192B 原文传输 |
| 缓存头 | `/_astro/` 可 `immutable, max-age=31536000` | 无 `Cache-Control` |
| 404 | 站点自己的 `404.html` | frp 的纯文本 `404 page not found` |
| 依赖 | 板上必须有 nginx | 只要一个静态二进制，rootfs 干净也能跑 |
| 适用 | 板上已有 nginx（本项目现状） | 板上没有包管理器、装不了 nginx |

**当前线上使用的是方案 A。** 两者可随时互换，见第 5 节。

---

## 2. 原理

### 2.1 为什么必须由板子主动外连

板子挂在 `192.168.137.0/24`（Windows 网络共享 ICS）后面，没有公网 IP，路由器也不做端口映射。
外部无法主动连到它，所以方向反过来：**板子上的 frpc 主动连服务器的 frps:7000**，
这条 TCP 连接保持长活，服务器收到的请求顺着这条已建立的连接回灌给板子。
这就是"反向隧道"——复用的是内网主动发起的连接。

```
浏览器 ──HTTPS:443──► 服务器 nginx ──► 127.0.0.1:8080 (frps)
                                          │  已建立的长连接
                                          ▼
                                     板子 frpc ──► 本机 80 (nginx) 或直接读磁盘
```

### 2.2 为什么 TLS 必须在服务器终结

证书和私钥放在板子上有三个问题：A7 跑 TLS 握手开销大；Let's Encrypt 证书 90 天一续，
续期要写 SD 卡；板子容易断电、重刷系统，私钥丢失风险高。
所以 HTTPS 在服务器上解开，隧道内部走明文 HTTP——隧道本身有 frp 的 TLS 层加密。

### 2.3 为什么 `proxyBindAddr` 必须是 `127.0.0.1`

frps 打通隧道后，会在**服务器上真的监听** 8080 端口。如果绑 `0.0.0.0`，
任何人都能访问 `http://服务器IP:8080`，绕过 nginx 和 HTTPS 直接拿到站点内容，证书等于白做。
绑 `127.0.0.1` 后，只有同机的 nginx 能反代过去，外部不可达。

### 2.4 frp 的两种工作模式

| 模式 | 配置 | frpc 的角色 |
| --- | --- | --- |
| 端口转发 | `localIP` + `localPort` | 只做搬运，把请求转给本机某个端口 |
| 插件 | `[proxies.plugin]` | 自己就是服务端，`static_file` 直接读目录发文件 |

方案 A 用前者，方案 B 用后者。插件模式不需要任何额外进程，但也因此丢掉了 nginx 的
压缩、缓存头、错误页这些能力。

### 2.5 为什么不在板上构建

Astro 构建峰值内存超过 500MB，而板子只有 494MB。在板上 `npm install` / `npm run build`
必然 OOM，严重时会把 sshd 一起挤掉导致失联。所以构建只在本机做，板上只接收 `dist/` 产物。

---

## 3. 服务器侧（两种方案通用，只做一次）

### 3.1 勘察

```bash
ss -lntp | grep -E ':(80|443|7000)\b'      # 端口占用
ls -l /etc/nginx/sites-enabled/            # 已有站点
ls -l /etc/letsencrypt/live/ 2>&1          # 证书是否存在
systemctl is-active nginx
```

### 3.2 临时 ACME 配置 + 签证书

443 段若引用不存在的证书，nginx 会拒绝启动，导致 80 也起不来，签证书陷入死锁。
先只加载一份 80 端口配置：

```bash
mkdir -p /var/www/letsencrypt
cat > /etc/nginx/sites-available/acme-temp <<'CONF'
server {
    listen 80;
    server_name board.nexuslink.icu;
    location ^~ /.well-known/acme-challenge/ { root /var/www/letsencrypt; }
    location / { return 200 "acme-ok\n"; }
}
CONF

rm -f /etc/nginx/sites-enabled/default
mkdir -p /etc/nginx/sites-enabled-bak
mv /etc/nginx/sites-enabled/board /etc/nginx/sites-enabled-bak/ 2>/dev/null
ln -sfn /etc/nginx/sites-available/acme-temp /etc/nginx/sites-enabled/acme-temp
nginx -t && systemctl restart nginx
curl -s -H 'Host: board.nexuslink.icu' http://127.0.0.1/     # 期望 acme-ok
```

```bash
certbot certonly --webroot -w /var/www/letsencrypt \
  -d board.nexuslink.icu --agree-tos --no-eff-email -n
```

> 用 `certonly --webroot` 而不是 `certbot --nginx`：后者会自动改写 nginx 配置，改了什么看不见。

### 3.3 正式 443 反代

```bash
cat > /etc/nginx/sites-available/board <<'CONF'
server {
    listen 80;
    server_name board.nexuslink.icu;
    location ^~ /.well-known/acme-challenge/ { root /var/www/letsencrypt; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name board.nexuslink.icu;

    ssl_certificate     /etc/letsencrypt/live/board.nexuslink.icu/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/board.nexuslink.icu/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;
    server_tokens       off;

    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    location / { proxy_pass http://127.0.0.1:8080; }
}
CONF

rm -f /etc/nginx/sites-enabled/acme-temp
ln -sfn /etc/nginx/sites-available/board /etc/nginx/sites-enabled/board
mv /etc/nginx/sites-enabled-bak/* /etc/nginx/sites-enabled/ 2>/dev/null
nginx -t && systemctl reload nginx
```

此时访问应是 **502**——TLS 已通，隧道那端还没连上，属正常。

### 3.4 frps

```bash
cd /tmp
curl -fsSL -o frp.tar.gz \
  https://ghfast.top/https://github.com/fatedier/frp/releases/download/v0.58.1/frp_0.58.1_linux_amd64.tar.gz
tar xzf frp.tar.gz && install -m 755 frp_0.58.1_linux_amd64/frps /usr/local/bin/frps

mkdir -p /etc/frp
cat > /etc/frp/frps.toml <<'CONF'
bindPort = 7000
proxyBindAddr = "127.0.0.1"

auth.method = "token"
auth.token = "改成你自己的随机串（openssl rand -hex 16）"

allowPorts = [{ start = 8080, end = 8089 }]
CONF

cat > /etc/systemd/system/frps.service <<'UNIT'
[Unit]
Description=frp server
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frps -c /etc/frp/frps.toml
Restart=always

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload && systemctl enable --now frps
ss -lntp | grep 7000
```

云控制台安全组需放行 `80 / 443 / 7000`。服务器上 `ufw` 关了不代表能通。

---

## 4. 板子侧

### 4.1 共同前置

板子没有 systemd（PID 1 是 `init`），自启用 init 脚本 + runlevel 软链。

```bash
mkdir -p /etc/frp /usr/local/bin /var/www/site
cd /usr/local/bin
wget -O frpc http://112.124.58.134/.well-known/acme-challenge/dl/frpc   # arm 静态二进制
chmod +x frpc && ./frpc -v
```

`/etc/frp/frpc.toml` 的公共部分（两方案一致）：

```toml
serverAddr = "112.124.58.134"
serverPort = 7000
auth.method = "token"
auth.token = "与 frps.toml 完全一致"

loginFailExit = false

transport.tls.enable = true
transport.heartbeatInterval = 10
transport.heartbeatTimeout = 30
transport.tcpMux = true
```

启动与自启：

```bash
install -m 755 /path/frpc.init /etc/init.d/frpc    # 仓库 deploy/frpc.init
ln -sf /etc/init.d/frpc /etc/rc5.d/S99frpc
/etc/init.d/frpc start
```

### 4.2 方案 A · 板上 nginx

**① 主配置必须包含 conf.d**

Yocto 自带的 `/etc/nginx/nginx.conf` 没有这一行，不补的话站点配置放进去也不生效
（现象：80 端口仍返回 nginx 默认页）。

```nginx
user  www-data;
worker_processes  1;
error_log  /var/log/nginx/error.log warn;
pid        /run/nginx/nginx.pid;

events { worker_connections 512; }

http {
    include       mime.types;
    default_type  application/octet-stream;
    sendfile      on;
    tcp_nopush    on;
    keepalive_timeout 15;

    include /etc/nginx/conf.d/*.conf;
}
```

**② 站点配置**：用仓库 `deploy/nginx.conf` 放到 `/etc/nginx/conf.d/site.conf`。

要点：

| 配置 | 原因 |
| --- | --- |
| `listen 80 default_server;` 单行，不写 `[::]:80` | 板上 nginx 1.8.1 没编译 IPv6，写会报 `INET6 sockets are not supported` |
| `gzip off; gzip_static on;` | 发预压缩 `.gz`，A7 不做运行时压缩 |
| `try_files $uri $uri/ $uri.html =404;` | 兼容 `trailingSlash: 'always'` 的目录式产物 |
| `error_page 404 /404.html;` | 用站点自己的 404 页 |
| `access_log off;` | 减少 SD 卡写放大 |

**③ 预压缩**（`gzip_static` 的前提，每次发布都要重做）

```bash
cd dist
find . -type f \( -name '*.html' -o -name '*.css' -o -name '*.xml' \
  -o -name '*.json' -o -name '*.svg' \) -exec gzip -9 -k -f {} \;
```

**④ 检查并重载**

```bash
/usr/sbin/nginx -t && /usr/sbin/nginx -s reload
curl -s -H 'Accept-Encoding: gzip' -D - -o /dev/null http://127.0.0.1/ | grep -i content-encoding
```

**⑤ frpc 指向本机 80**：`deploy/frpc.toml`

```toml
[[proxies]]
name = "board-web"
type = "tcp"
localIP = "127.0.0.1"
localPort = 80
remotePort = 8080
```

### 4.3 方案 B · frpc 静态插件

不需要 nginx，站点文件放好即可。`deploy/frpc-static.toml`：

```toml
[[proxies]]
name = "board-web"
type = "tcp"
remotePort = 8080

[proxies.plugin]
type = "static_file"
localPath = "/var/www/site"
```

`static_file` 每次请求直接读磁盘，换文件即时生效，**不需要 reload 或重启 frpc**。
也不需要预压缩——该插件不支持 `gzip_static`。

---

## 5. 两个方案互切

只改 `/etc/frp/frpc.toml`，然后 `/etc/init.d/frpc restart`，中断约 2 秒。

| 方向 | 改动 | 前提 |
| --- | --- | --- |
| B → A | 把 `[proxies.plugin]` 段删掉，加 `localIP = "127.0.0.1"` + `localPort = 80` | 板上 nginx 已跑起来且 `curl 127.0.0.1` 能看到站点 |
| A → B | 反过来，把 `localIP/localPort` 换成 `[proxies.plugin] static_file` | 站点文件在 `localPath` 下 |

切换顺序建议：**先把目标方案在本机端口上跑通（`curl 127.0.0.1`），再改 frpc**。
这样切换失败时线上只是短暂 502，回滚只需改回配置。

---

## 6. 发布更新

本机构建（Windows PowerShell 即可）：

```powershell
cd "D:\Documents\大三上\嵌入式系统\项目\embeded_web"
npx astro build
```

方案 A 还要预压缩（第 4.2 ③ 那条 `find ... gzip`），然后打包上传：

```bash
tar czf site.tar.gz -C dist .
scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    site.tar.gz root@<板子IP>:/tmp/
```

板上替换：

```bash
ls /var/www/site                      # 先确认路径，再执行下一条
rm -rf /var/www/site/*
tar xzf /tmp/site.tar.gz -C /var/www/site
```

方案 A 无需 reload（静态文件每次请求都从磁盘读）；只有改 **nginx 配置**才需要 `nginx -s reload`。

---

## 7. 验收

```bash
# 板内
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/
# 服务器
ss -lntp | grep -E ':(80|443|7000|8080)\b'      # 7000 与 127.0.0.1:8080 属 frps
# 外网
curl --noproxy '*' -s -o /dev/null -w "%{http_code} %{size_download}B %{time_total}s\n" \
  https://board.nexuslink.icu/
```

逐页检查时 13 条路由全部应为 200：`/ /lab/ /project/ /course-design/ /docs/ /notes/
/source/ /gallery/ /status/ /about/ /rss.xml /status.json /favicon.svg`。

证书续期：

```bash
certbot renew --dry-run --no-random-sleep-on-renew
```

---

## 8. 坑与排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 重启后站点 502，手动启动就好 | `S99frpc` 早于网络就绪，frpc 默认登录失败即退出 | `frpc.toml` 加 `loginFailExit = false` |
| `nginx: [emerg] INET6 sockets are not supported` | 板上 nginx 未编译 IPv6 | 删掉 `listen [::]:80` |
| 放了 site.conf 但仍是 nginx 默认页 | `nginx.conf` 缺 `include conf.d/*.conf` | 补上并 reload |
| 页面没压缩（无 `Content-Encoding`） | 缺 `.gz` 或 `gzip_static` 未启用 | 重新预压缩 |
| `Cache-Control` 出现两个 | `expires` 与 `add_header` 同时作用 | 只留 `add_header` |
| `no matching host key type` | 板上 dropbear 只提供 `ssh-rsa` | 客户端加 `-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa` |
| `nginx: command not found`（ssh 下） | 非登录 shell 的 PATH 不含 `/usr/sbin` | 用绝对路径 `/usr/sbin/nginx` |
| `pkill -f 'frpc -c'` 无输出、退出码 0 | `-f` 匹配到执行命令的 shell 自身 | 用 `/etc/init.d/frpc stop` |
| 外网能直连 `:8080` | `proxyBindAddr` 不是 `127.0.0.1` | 改回后重启 frps |
| 板子 IP 变了、SSH 连不上 | ICS 的 DHCP 重新分配 | 串口 `ip -4 addr show eth0` 查，或用 `arp -a` 找 MAC |
| tar 提示时间戳在未来 | 板子 RTC 掉电不保时 | `date -s` 校时；不影响服务 |

---

## 9. 文件清单

**仓库 `deploy/`**

| 文件 | 用途 |
| --- | --- |
| `DEPLOY.md` | 本文档 |
| `nginx.conf` | 方案 A 的板上站点配置 |
| `frpc.toml` | 方案 A 的 frpc 配置（转发本机 80） |
| `frpc-static.toml` | 方案 B 的 frpc 配置（静态插件） |
| `frps.toml` | 服务器端 frps 配置 |
| `frpc.init` | 板上 frpc 的 init 脚本 |
| `deploy.sh` | 有 rsync 时的快捷发布（build + gzip + rsync + reload） |
| `status-collector.sh` | 可选，生成 `status.json` 状态快照 |

**服务器**

| 路径 | 说明 |
| --- | --- |
| `/etc/frp/frps.toml`、`/etc/systemd/system/frps.service` | frps 配置与常驻 |
| `/etc/nginx/sites-available/board` | 80 跳转 + 443 反代 |
| `/etc/letsencrypt/live/board.nexuslink.icu/` | 证书（自动续期） |

**开发板**

| 路径 | 说明 |
| --- | --- |
| `/usr/local/bin/frpc` | frp 客户端 |
| `/etc/frp/frpc.toml` | 当前生效的 frpc 配置 |
| `/etc/init.d/frpc` + `/etc/rc5.d/S99frpc` | 启停与自启 |
| `/etc/nginx/nginx.conf`、`/etc/nginx/conf.d/site.conf` | 方案 A 的 nginx 配置 |
| `/var/www/site/` | 站点产物（含 `.gz`） |
