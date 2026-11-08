# 公网服务器手动初始化（不依赖任何脚本）

本文是公网服务器的手动初始化流程。全部命令逐条手敲，每条都说明「改了什么、怎么验证」。
只需要做一次，之后每次发布只在本机跑 `npm run deploy`。

（早期版本提供过一个一键脚本，现已移除：脚本把「改了什么」藏起来了，出问题时反而更难查。）

**环境假设**：Ubuntu 20.04 / 22.04 / 24.04，有 sudo 权限，域名已能解析到本机公网 IP。
其他发行版把 `apt` 换成对应包管理器即可，Nginx 配置完全通用。

---

## 0. 动手前的四项检查

这四项任何一项不对，后面都会以「莫名其妙失败」的形式出现，先花两分钟确认。

```bash
# ① 域名解析是否已经指向本机
dig +short imx6ull.example.com
# 期望输出本机公网 IP。没有输出或不是本机 IP → 先去域名商改 A 记录，等生效（通常几分钟）

# ② 本机公网 IP 是多少（云主机是 NAT 的，网卡上看到的可能是内网地址）
curl -s ifconfig.me; echo

# ③ 80 / 443 是否被别的服务占着
sudo ss -lntp | grep -E ':(80|443)\b'
# 没有输出 = 空闲。有输出 → 记下是什么进程，后面可能要先停掉或复用它

# ④ 系统版本
lsb_release -a 2>/dev/null || cat /etc/os-release | head -3
```

还要去**云控制台的安全组**里放行 `80`、`443`、`7000`（TCP）。
这一步最容易被忽略：本机 `ufw` 关了也没用，安全组是云厂商在外层拦的。

> **如果这台机器上已经跑着别的网站（尤其是 Docker 容器），80 / 443 很可能已经被占用。**
> 先跳到文末「附二」，判断属于哪种情况，再回来按顺序执行。

---

## 1. 安装 Nginx 与 certbot

```bash
sudo apt update
sudo apt install -y nginx certbot
```

只用 `certbot` 本体（不装 `python3-certbot-nginx`），因为下面全程手写配置，
不需要 certbot 去自动改写 Nginx 文件——改了什么自己能看见，出问题也好排查。

```bash
nginx -v
# nginx version: nginx/1.24.0 (Ubuntu)   ← 记下版本，第 6 步要用到 http2 写法

sudo systemctl enable --now nginx
sudo systemctl status nginx --no-pager
curl -I http://127.0.0.1
# 期望 HTTP/1.1 200 OK（此时是 Nginx 默认欢迎页）
```

> 如果这台服务器上已经跑着别的站点（比如以前的 Vue 项目），跳过安装，直接往下走。
> 注意别覆盖它已有的 `server` 块，本教程用的是独立文件 `board-site`。

---

## 2. 创建两个目录

```bash
# ① ACME 校验目录：certbot 把校验文件写到这里，Nginx 通过 URL 暴露出去
sudo mkdir -p /var/www/letsencrypt

# ② 隧道入口占位（可选，仅用于第 9 步验证 TLS 是否通，不需要可以跳过）
sudo mkdir -p /var/www/board-site
echo 'board tunnel placeholder' | sudo tee /var/www/board-site/index.html

sudo chown -R www-data:www-data /var/www
```

站点文件本身**不放**在这台服务器上，它放在开发板上——服务器只做证书与转发。
这里只准备 ACME 校验所需的目录。

---

## 3. 写 80 端口的站点配置

```bash
sudo nano /etc/nginx/sites-available/board-site
```

粘贴下面内容，把 `imx6ull.example.com` 换成你的域名：

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name imx6ull.example.com;

    # ACME HTTP-01 校验：必须先于下面的 301，否则续期会被跳转打断
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type "text/plain";
    }

    # 先放占位页，验证 Nginx 与域名是否配对了；第 6 步之后改成反代
    location / {
        root /var/www/board-site;
        try_files $uri $uri/ =404;
    }
}
```

启用并检查：

```bash
sudo ln -s /etc/nginx/sites-available/board-site /etc/nginx/sites-enabled/board-site
sudo nginx -t
sudo systemctl reload nginx
```

**验证**（浏览器或命令行）：

```bash
curl -I http://imx6ull.example.com
# 期望：HTTP/1.1 200 OK，且页面内容为 board tunnel placeholder
```

如果这里返回的是 Nginx 默认欢迎页，说明默认站点抢走了匹配：

```bash
ls -l /etc/nginx/sites-enabled/
# 若有 default，删掉软链后 reload（不影响本教程的站点）
sudo rm -f /etc/nginx/sites-enabled/default
sudo systemctl reload nginx
```

> 国内云主机如果域名未备案，80 端口访问会被拦截。这种情况可以跳过 80 端口验证，
> 直接从第 4 步用 DNS 校验（见第 4 步末的备选方案），443 通常仍可用。

---

## 4. 申请证书

用 `webroot` 方式，只申请证书、不改 Nginx 配置：

```bash
sudo certbot certonly --webroot \
  -w /var/www/letsencrypt \
  -d imx6ull.example.com \
  -m you@example.com \
  --agree-tos --no-eff-email -n
```

成功后会打印证书路径，并写入 `/etc/letsencrypt/live/imx6ull.example.com/`：

```bash
sudo ls -l /etc/letsencrypt/live/imx6ull.example.com/
# cert.pem  chain.pem  fullchain.pem  privkey.pem
```

**验证**：

```bash
openssl x509 -in /etc/letsencrypt/live/imx6ull.example.com/fullchain.pem \
  -noout -subject -dates -issuer
# 能看到 subject=CN = imx6ull.example.com 与有效期（90 天）
```

如果报 `Timeout during connect` / `DNS problem` / `404`：

- A 记录没生效 → 回到第 0 步 ①
- 80 端口被拦 → 安全组 / ufw / 备案
- 校验路径不对 → 确认第 3 步的 `location ^~` 里的 `root` 与 `-w` 完全一致

> **备选**：80 端口确实不可用时改用 DNS 校验：
> `sudo certbot certonly --manual --preferred-challenges dns -d imx6ull.example.com`
> 按提示到域名商加一条 TXT 记录。缺点是后面续期没法自动，适合应急。

---

## 5. 先装 frps，再改配置（顺序别反）

frps 装好并跑起来后，第 6 步的 `proxy_pass` 才有东西可连。

```bash
# 按 CPU 架构选包：x86_64 → amd64，ARM 服务器 → arm64
uname -m

FRP_VER=0.58.1
cd /tmp
wget https://github.com/fatedier/frp/releases/download/v${FRP_VER}/frp_${FRP_VER}_linux_amd64.tar.gz
tar xzf frp_${FRP_VER}_linux_amd64.tar.gz
sudo install -m 755 frp_${FRP_VER}_linux_amd64/frps /usr/local/bin/frps
frps -v
```

写服务端配置：

```bash
sudo mkdir -p /etc/frp
sudo nano /etc/frp/frps.toml
```

```toml
bindPort = 7000
proxyBindAddr = "127.0.0.1"     # 关键：8080 只在本机可见

auth.method = "token"
auth.token = "把这里换成自己的随机串"

# 允许板子注册的端口范围，防止被注册到意外端口
allowPorts = [{ start = 8080, end = 8089 }]
```

生成 token（两边要一致）：

```bash
openssl rand -hex 16
```

> `allowPorts` 在部分 frp 版本里字段名有变化，若启动报「未知字段」就删掉这一行，
> 靠 `proxyBindAddr = "127.0.0.1"` 与 token 已经足够。

手写 systemd 单元（不是脚本，就是一份服务描述）：

```bash
sudo nano /etc/systemd/system/frps.service
```

```ini
[Unit]
Description=frp server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frps -c /etc/frp/frps.toml
Restart=always
RestartSec=5s
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

启动并确认端口：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now frps
sudo systemctl status frps --no-pager
sudo journalctl -u frps -n 20 --no-pager

sudo ss -lntp | grep 7000
# 期望：LISTEN 0 ... 0.0.0.0:7000 ... frps
```

此时 8080 **还没有**监听，因为板子还没连上来——这是正常的，不是错误。

---

## 6. 把 80 端口改成反代，并加上 443

现在证书有了、frps 有了，可以写最终配置了。

```bash
sudo nano /etc/nginx/sites-available/board-site
```

整份替换为：

```nginx
# ---------- 80：只做 ACME 校验与跳转 ----------
server {
    listen 80;
    listen [::]:80;
    server_name imx6ull.example.com;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type "text/plain";
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# ---------- 443：证书 + 反代到隧道 ----------
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name imx6ull.example.com;

    ssl_certificate     /etc/letsencrypt/live/imx6ull.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/imx6ull.example.com/privkey.pem;

    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;

    # 站点很小，直接透传即可；不缓存 HTML 便于改完立即生效
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;

    location / {
        proxy_pass http://127.0.0.1:8080;   # 与 frpc 的 remotePort 一致
    }
}
```

> `listen 443 ssl http2;` 在 Nginx ≥ 1.25.1 会提示 deprecated。若你的版本是 1.25+，
> 改成 `listen 443 ssl;` 并在 server 块里加一行 `http2 on;`。

```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## 7. 逐层验证（重要，别跳）

板子还没配置时，这里能验证到「TLS 通、隧道缺」；等板子连上后再验一遍全链路。

```bash
# ① 80 是否正确跳转
curl -I http://imx6ull.example.com
# 期望：301 Moved Permanently，Location: https://...

# ② 证书是否生效
curl -vkI https://imx6ull.example.com 2>&1 | grep -E 'SSL connection|subject|issuer'
# 期望：SSL connection using TLSv1.3 / subject: CN=imx6ull.example.com

# ③ 业务是否通（板子未连上时这里必然是 502，属正常）
curl -I https://imx6ull.example.com
# 期望：502 Bad Gateway  ← 说明 TLS 与 Nginx 都正常，只差隧道另一端

# ④ 隧道端口状态
sudo ss -lntp | grep -E ':(7000|8080)\b'
# 7000 应一直有；8080 要等 frpc 连上后才出现
```

---

## 8. 续期检查（一次性确认，之后自动）

Ubuntu 装 certbot 时会自带 systemd timer：

```bash
systemctl list-timers | grep certbot
# 期望：certbot.timer  active (waiting)

sudo certbot renew --dry-run
# 期望：Congratulations, all simulated renewals succeeded
```

dry-run 通过就说明 90 天后会自动续。续期前不需要停 Nginx——这正是第 3 步
把 `.well-known` 单独留出来的原因。

---

## 9. 板子接上之后

在开发板上按 `deploy/frpc.toml` 配好 `serverAddr` 与 `token`，启动 frpc：

```bash
sudo systemctl enable --now frpc
sudo journalctl -u frpc -n 30 --no-pager
```

回到服务器验证：

```bash
sudo ss -lntp | grep 8080
# 期望：出现 127.0.0.1:8080（因为 proxyBindAddr 绑的是本机）

curl -I https://imx6ull.example.com
# 期望：HTTP/1.1 200 OK，Server 头是开发板上那个 Nginx
```

看到 200 就通了。之后每次改内容只在本机跑：

```bash
BOARD=root@192.168.1.50 npm run deploy
```

---

## 附：故障对照表

| 现象 | 大概率原因 | 排查命令 |
| --- | --- | --- |
| 浏览器连不上，curl 超时 | 云安全组没放行 80/443 | 控制台看安全组；`sudo ufw status` |
| 80 打开了但显示 Nginx 欢迎页 | `sites-enabled/default` 抢匹配 | `ls -l /etc/nginx/sites-enabled/` |
| 一直 502 | frps 没起 / frpc 未连 / 板上 Nginx 没起 | `journalctl -u frps -n 50`；板上 `curl -I 127.0.0.1` |
| 502 且 frps 日志有 `login to server failed` | 两边 token 不一致 | 对比 `frps.toml` 与 `frpc.toml` |
| certbot 报 404 | ACME location 路径与 `-w` 不一致 | 手建一个测试文件再用 `curl` 访问 |
| certbot 报 DNS problem | A 记录没生效或解析到了旧 IP | `dig +short 域名` |
| frps 启动即退出 | 配置字段名不被当前版本支持 | `journalctl -u frps -n 50`，按提示删字段 |
| 8080 起了但从外网能直连 | `proxyBindAddr` 忘了写 | 必须为 `127.0.0.1` |

常用日志位置：

```bash
sudo journalctl -u nginx -n 50 --no-pager
sudo tail -n 50 /var/log/nginx/error.log
sudo journalctl -u frps -f
sudo journalctl -u frpc -f          # 在开发板上执行
```

---

## 附二：80 / 443 已被 Docker 或其他站点占用

一台机器上只能有一个进程监听 80 / 443。先判断是谁占着：

```bash
sudo ss -lntp | grep -E ':(80|443)\b'
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
```

三种输出对应三条路：

| `ss` 输出的进程 | 含义 | 走哪个方案 |
| --- | --- | --- |
| `nginx` / `apache2` / `caddy` | 宿主机上已有 Web 服务在监听 | **方案 A**（直接加 server 块） |
| `docker-proxy` / `dockerd` | 容器用 `-p 80:80` 占用了端口 | **方案 A**（推荐）或方案 B |
| 具体服务进程名（不是 docker-proxy） | 容器用了 `network_mode: host` | **方案 B** |

### 方案 A：宿主机 Nginx 统一接管 80 / 443（推荐）

思路：让现有容器只监听本机端口，前面由宿主机 Nginx 统一做 TLS 与路由。
好处是证书只在一个地方管，多个域名互不影响，排错时链路清晰。

> 如果目标服务已经不用容器了（systemd 直接跑进程），这一节同样适用：
> 把它下面的 `127.0.0.1:8081` 换成该进程实际监听的端口（例如 `3180`），
> 并确保它只绑 `127.0.0.1`，不要绑 `0.0.0.0`。

**① 让现有容器退到本机端口**

```yaml
# docker-compose.yml
services:
  old-site:
    ports:
      - "127.0.0.1:8081:80"     # 原本可能是 "80:80" 或 "0.0.0.0:80:80"
```

```bash
docker compose up -d old-site
sudo ss -lntp | grep -E ':(80|8081)\b'
# 80 应已释放，8081 出现且只绑 127.0.0.1
```

**② 装宿主机 Nginx 并停掉默认站点**

```bash
sudo apt install -y nginx
sudo rm -f /etc/nginx/sites-enabled/default
```

**③ 给原有站点补一个 server 块**（域名保持不变）

```nginx
# /etc/nginx/sites-available/old-site
server {
    listen 80;
    listen [::]:80;
    server_name 原来的域名;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type "text/plain";
    }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name 原来的域名;

    ssl_certificate     /etc/letsencrypt/live/原来的域名/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/原来的域名/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

原来证书如果只存在于容器里（比如 Caddy / nginx-proxy 的卷），拷出来复用：

```bash
docker cp 容器名:/etc/letsencrypt/live/原来的域名 ./cert/
# 或直接用 webroot 在宿主机重签一张，更省事
```

**④ 再给 board 子域名加一个 server 块**

就是正文第 6 步那份配置，只换 `server_name`、证书路径和 `proxy_pass 127.0.0.1:8080`。
两个站点都**不要**写 `default_server`，靠 `server_name` 精确匹配即可互不干扰。

**⑤ 统一验证**

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -I https://原来的域名     # 应仍是 200
curl -I https://board.xxx.icu  # 板子未连上时 502
```

### 方案 B：反代写在容器内部

不想动现有容器时才用这条路，代价是要处理三件麻烦事。

**① 容器里访问不到宿主机的 `127.0.0.1`**

`127.0.0.1` 在容器网络命名空间里指的是容器自己。要用宿主机的 docker0 地址：

```bash
ip -4 addr show docker0 | grep inet      # 通常是 172.17.0.1
```

或者启动容器时加：

```bash
docker run --add-host=host.docker.internal:host-gateway ...
```

**② frps 的绑定地址必须跟着改**

因为上面的原因，`proxyBindAddr = "127.0.0.1"` 时容器连不上 8080，改成 docker0 地址：

```toml
# /etc/frp/frps.toml
proxyBindAddr = "172.17.0.1"
```

这样宿主机与容器都能访问，公网仍然到不了（`172.17.0.1` 不是公网接口，安全组也放不出去）。
容器内 Nginx 相应写 `proxy_pass http://172.17.0.1:8080;`。

**③ 证书要自己想办法**

证书在宿主机上，容器里读不到。两种做法：把
`/etc/letsencrypt/live/board.xxx.icu/` 用 `-v` 挂载进容器，或者改用 Caddy / traefik 让它自己签发。

改完重启容器内 Nginx：

```bash
docker exec 容器名 nginx -t
docker exec 容器名 nginx -s reload
```

### 方案 C：让现有 Caddy 兼任入口（机器上已经在跑 Caddy 反向代理时最省事）

识别特征：`docker ps` 里有 Caddy，且它的端口映射是
`127.0.0.1:8081:80` / `127.0.0.1:8082:443` —— 说明 80 / 443 上另有一个入口在把流量转给它。
这种情况下不要再装宿主机 Nginx 去抢端口，**直接让 Caddy 多接一个域名**，证书它自己会签。

**① 确认 Caddy 收得到任意 Host**

前置入口如果按 `server_name` 精确匹配，新域名会落到别的 server 块里去：

```bash
curl -sI -H 'Host: board.xxx.icu' http://127.0.0.1/ | head -3
```

返回的是 Caddy 的响应（通常是 308 跳转或 404 from Caddy）就没问题；
如果返回前置代理自己的 404，需要把它的 `server_name` 放宽为
`server_name xxx.icu board.xxx.icu;` 或 `server_name _;`。

**② Caddyfile 追加一个站点块**（写死域名最省事，不必动原来的块）

```caddyfile
board.xxx.icu {
	encode zstd gzip

	reverse_proxy host.docker.internal:8080 {
		header_up Host {host}
		header_up X-Forwarded-Proto {scheme}
	}
}
```

**③ 给 caddy 服务加 hosts 映射**

`host.docker.internal` 在 Linux 上默认不存在，必须显式声明：

```yaml
  caddy:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

**④ frps 必须改成容器能连的地址**

这是整条链路唯一的技术难点。`proxyBindAddr = "127.0.0.1"` 时，容器里的
`127.0.0.1` 指的是容器自己，**连不到宿主机上的 8080**。二选一：

```toml
# 做法一：绑 docker 网桥地址（推荐，公网天然不可达）
proxyBindAddr = "172.18.0.1"
```

```bash
# 先查实际网关地址，别硬抄 172.18.0.1
docker network inspect <项目名>_default -f '{{range .IPAM.Config}}{{.Gateway}} {{end}}'
```

```toml
# 做法二：绑 0.0.0.0，再用防火墙把 8080 关在私有网段内
proxyBindAddr = "0.0.0.0"
```

```bash
sudo iptables -I DOCKER-USER -p tcp --dport 8080 ! -s 172.16.0.0/12 -j DROP
```

`DOCKER-USER` 链在 Docker 端口转发之前生效，这条规则表示「只允许私有网段访问 8080」。
注意重启后规则会丢，要持久化（`iptables-persistent` 或 systemd 单元）。

**⑤ 重建并重启**

```bash
docker compose up -d caddy
sudo systemctl restart frps
docker logs --tail 80 <caddy 容器名> 2>&1 | grep -i -E 'certificate|obtain'
# 期望看到为 board.xxx.icu 签发证书的日志
```

**⑥ 验证**

```bash
sudo ss -lntp | grep 8080                 # 应听在网桥地址或 0.0.0.0
curl -I https://board.xxx.icu             # 板子未连上时 502；连上后 200
```

Caddy 的证书全部存在 `caddy_data` 卷里，两个域名共用一套自动续期，
备份这个卷就等于备份了证书。

### 附二的注意事项

- **Docker 发布的端口会绕过 ufw**（走 `DOCKER-USER` 链）。`ufw status` 显示放行不代表真的放行，
  判断是否可达一律以云安全组 + 实际 `curl` 为准。
- 一台机器上 `default_server` 只能有一个，重复声明会导致 Nginx 启动失败。
- 域名走 Cloudflare 时，先把子域名设成**灰云（DNS only）**签证书，签完再开橙云代理；
  橙云期间源站必须能被 CF 回源访问到 443。
- 子域名与主域名互不影响，已有站点继续用自己的 `server_name`，不需要改它的配置。

