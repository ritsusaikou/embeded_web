# 部署到 i.MX6ULL + frp 反代到 board.nexuslink.icu · 手操手册

全程**不使用** `deploy/` 下的任何 `.sh`（deploy.sh / server-init.sh / status-collector.sh），  
每条命令自己敲。下面所有命令都可在终端里独立执行。

## 已在本机做完的两件事

| 改动   | 文件                                                         | 说明                                  |
| ---- | ---------------------------------------------------------- | ----------------------------------- |
| 站点域名 | `astro.config.mjs` → `site: 'https://board.nexuslink.icu'` | 决定 canonical / RSS / sitemap 里的绝对地址 |
| 重新构建 | `dist/`（23 个页面、29 个文件、463 KB）                              | 已用 `npx astro build` 重建，HTML 里已是新域名 |

## 参数表（下文用这些值，按自己的改）

| 项           | 值                                    |
| ----------- | ------------------------------------ |
| 域名          | `board.nexuslink.icu`                |
| 服务器公网 IP    | `112.124.58.134`                     |
| 板子 IP / SSH | `192.168.1.50` / `root@192.168.1.50` |
| 板上站点目录      | `/var/www/site`                      |
| frp token   | `3edc37ef6bca4f276c4d0c6ba3c9421d`   |
| 隧道端口 / 映射端口 | `7000` / `8080`                      |

> token 已写进 `deploy/frpc.toml`。若这个仓库要公开，先换成自己的：  
> `openssl rand -hex 16`，两边的 toml 必须一模一样。



---

## 第 0 步 · DNS（阻塞项，先做）

我用阿里 223.5.5.5、DNSPod 119.29.29.29、Google 8.8.8.8 三家查过，  
`board.nexuslink.icu` 目前是 **NXDOMAIN（不存在）**，`nexuslink.icu` 则正常解析到 `112.124.58.134`。  
也就是说：**子域名的 A 记录还没加**，不加的话 certbot 一定报 `DNS problem`。

去域名商控制台加一条记录：

| 类型 | 主机记录    | 记录值              | TTL |
| -- | ------- | ---------------- | --- |
| A  | `board` | `112.124.58.134` | 600 |

验证（回显应为服务器 IP）：

```bash
nslookup board.nexuslink.icu 223.5.5.5
```

> 用了 Cloudflare 的话，签证书期间先保持**灰云（DNS only）**，签完再开橙云。

---

## 第 1 步 · 本机：构建 + 预压缩

```bash
cd /d/Documents/大三上/嵌入式系统/项目/embeded_web
npx astro build
```

预压缩（板上 Nginx 用 `gzip_static` 直接发同名 `.gz`，省 A7 的 CPU）：

```bash
cd dist
find . -type f \( -name '*.html' -o -name '*.css' -o -name '*.xml' -o -name '*.json' -o -name '*.svg' \) -exec gzip -9 -k -f {} \;
```

> 这是一条命令，不是脚本。不想压缩也行：把第 4 步配置里的  
> `gzip_static on;` 改成 `gzip on;`，由板子实时压（每次请求多几毫秒 CPU）。

---

## 第 2 步 · 板子：先认环境

SSH 上去，四条命令确认前提：

```bash
uname -m && cat /etc/os-release | head -3 && ps -p 1 -o comm= && free -m | head -2
```

期望：`armv7l`、Debian/Ubuntu、`systemd`、内存 512 MB 左右。

> - 输出 `armv7l` → frp 用 `linux_arm` 包（Cortex-A7 是硬浮点 armhf）。
> - `ps -p 1` 是 `init` 而不是 `systemd`（Buildroot/Yocto 常见）→ 第 9 步的自启改用 `/etc/rc.local`。
> - 没有 `apt` 的 rootfs：nginx 用 `opkg install nginx` 或交叉编译；  
>   **frpc 不用编译**，它是静态 Go 程序，直接拷 arm 二进制就能跑。

---

## 第 3 步 · 板子：装 Nginx

```bash
apt update
apt install -y nginx
rm -f /etc/nginx/sites-enabled/default
systemctl enable --now nginx
curl -I http://127.0.0.1
```

> `rm` 那条别省。Debian 的默认站点在 `:80` 上声明了 `default_server`，  
> 第 4 步配置里也声明了一个，两个同时存在 Nginx 直接启动失败，  
> 报 `duplicate default server`。
>
> **板子上没有 `apt`（Buildroot / Yocto rootfs）→ 跳过本步和第 4 步**，
> 看文末「附一」：四条不依赖包管理器的路，其中两条完全不需要 Nginx。

---

## 第 4 步 · 板子：站点配置

```bash
mkdir -p /var/www/site
nano /etc/nginx/conf.d/site.conf
```

粘贴 `deploy/nginx.conf` 的内容（`listen 80 default_server` + `root /var/www/site` +  
`gzip_static on` + `access_log off`）。然后：

```bash
nginx -t
nginx -s reload
```

> **两个嵌入式特有的坑：**
>
> 1. 报 `socket() [::]:80 failed (97: Address family not supported)` —— 内核没编 IPv6。  
>    删掉 `listen [::]:80 default_server;` 这一行即可。
> 2. 报 `unknown directive "gzip_static"` —— 缺模块。  
>    要么 `apt install -y libnginx-mod-http-gzip-static`，要么把 `gzip_static on;` 换成 `gzip on;`。
>
> 顺手把 `/etc/nginx/nginx.conf` 里 `worker_connections` 改到 `512`：512 MB 内存、  
> 单核 A7 跑不满默认的 1024，调小更稳。`access_log` 在站点配置里已关，  
> 目的是减少 SD 卡写放大——这块卡是要用四年的。

---

## 第 5 步 · 本机：把产物推上板子

```bash
cd /d/Documents/大三上/嵌入式系统/项目/embeded_web

ssh root@192.168.1.50 "pwd && ls /var/www/site"
ssh root@192.168.1.50 "rm -rf /var/www/site/*"
tar -C dist -czf - . | ssh root@192.168.1.50 "tar -xzf - -C /var/www/site"
```

> 第一条是**核对**：确认目录真的是 `/var/www/site` 再敲第二条 `rm -rf`。  
> 不用 `deploy.sh` 里的 rsync，是因为 Windows 的 Git Bash 通常不带 rsync；  
> |  |  |  
> | --- | --- |  
> `tar | ssh` 只用 ssh，一条命令搞定，缺点是不做增量、必须先清空目录。  
> 本机若有 rsync（WSL / 装了 rsync 的 Git Bash），可换成：  
> `rsync -avz --delete dist/ root@192.168.1.50:/var/www/site/`

板上验证：

```bash
nginx -s reload
curl -I http://127.0.0.1/
curl -s http://127.0.0.1/ | head -5
```

看到 `200 OK` 就说明静态站点本身已经跑起来了，后面几步都是「怎么让公网看见它」。

---

## 第 6 步 · 服务器：80 端口先立住

```bash
ssh root@112.124.58.134
apt update && apt install -y nginx certbot
mkdir -p /var/www/letsencrypt
nano /etc/nginx/sites-available/board-site
```

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name board.nexuslink.icu;

    # ACME 校验必须先于下面的跳转，否则续期会被 301 打断
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type "text/plain";
    }

    location / {
        return 301 https://$host$request_uri;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/board-site /etc/nginx/sites-enabled/board-site
nginx -t && nginx -s reload
curl -I http://board.nexuslink.icu
```

> 这一步只允许返回 `301`（跳到 https），因为 443 还没配、隧道也还没通，  
> 浏览器打开会是错误页，**正常**。
>
> **如果 `apt install nginx` 之前 80/443 已被占用**（这台机器上跑着别的站或 Docker），  
> |  |  |  |  
> | --- | --- | --- |  
> 先 `ss -lntp | grep -E ':(80|443)\b'` 看是谁，再按 `deploy/SERVER-SETUP.md`  
> 文末「附二」的三种情况处理——直接装会抢端口，把原有站点打下来。

---

## 第 7 步 · 服务器：申请证书

```bash
certbot certonly --webroot -w /var/www/letsencrypt \
  -d board.nexuslink.icu -m 你的邮箱 \
  --agree-tos --no-eff-email -n
```

```bash
ls /etc/letsencrypt/live/board.nexuslink.icu/
```

> 用 `certonly --webroot` 而不是 `certbot --nginx`：后者会自己改写 Nginx 配置，  
> 改了什么看不见，出问题难查。webroot 只往目录里丢一个校验文件，配置始终在自己手里。

---

## 第 8 步 · 服务器：frps

```bash
uname -m      # 云主机一般 x86_64 → 用 amd64 包
cd /tmp
wget https://github.com/fatedier/frp/releases/download/v0.58.1/frp_0.58.1_linux_amd64.tar.gz
tar xzf frp_0.58.1_linux_amd64.tar.gz
install -m 755 frp_0.58.1_linux_amd64/frps /usr/local/bin/frps
frps -v
```

```bash
mkdir -p /etc/frp
nano /etc/frp/frps.toml
```

```toml
bindPort = 7000
proxyBindAddr = "127.0.0.1"      # 关键，见下方说明

auth.method = "token"
auth.token = "3edc37ef6bca4f276c4d0c6ba3c9421d"

allowPorts = [{ start = 8080, end = 8089 }]
```

```bash
nano /etc/systemd/system/frps.service
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

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now frps
ss -lntp | grep 7000
journalctl -u frps -n 20 --no-pager
```

> **`proxyBindAddr` 为什么必须是 `127.0.0.1`**：frp 打通后，映射出来的 8080 会真的  
> 在服务器上监听。不限制的话，外网可以直接 `http://服务器IP:8080` 绕过 Nginx  
> 和 HTTPS 访问站点，等于白做了证书。绑本机后，8080 只能被同一台机器上的  
> Nginx 反代到。
>
> **还要去云控制台安全组放行 `7000/TCP`**（80/443 一起放）。这一步最常被漏：  
> 服务器 `ufw` 关了也没用，安全组是云厂商在外层拦的。
>
> frp 0.52 之后只认 toml，网上大量老教程给的是 `frps.ini`，照抄会直接启动失败。

此时 `ss` 只该看到 7000，**8080 还没出现是对的**——板子还没连上来。

---

## 第 9 步 · 板子：frpc

```bash
cd /tmp
wget https://github.com/fatedier/frp/releases/download/v0.58.1/frp_0.58.1_linux_arm.tar.gz
tar xzf frp_0.58.1_linux_arm.tar.gz
install -m 755 frp_0.58.1_linux_arm/frpc /usr/local/bin/frpc
frpc -v
```

```bash
mkdir -p /etc/frp
nano /etc/frp/frpc.toml
```

直接抄 `deploy/frpc.toml`，重点是这几行：

```toml
serverAddr = "112.124.58.134"
serverPort = 7000
auth.method = "token"
auth.token = "3edc37ef6bca4f276c4d0c6ba3c9421d"
transport.tls.enable = true
transport.heartbeatInterval = 10
transport.heartbeatTimeout = 30

[[proxies]]
name = "board-web"
type = "tcp"
localIP = "127.0.0.1"
localPort = 80
remotePort = 8080
```

**先前台跑，看清日志再交给后台：**

```bash
date
frpc -c /etc/frp/frpc.toml
```

期望看到 `start proxy success`。然后 `Ctrl+C`，配开机自启：

```bash
nano /etc/systemd/system/frpc.service
```

```ini
[Unit]
Description=frp client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frpc -c /etc/frp/frpc.toml
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now frpc
journalctl -u frpc -n 30 --no-pager
```

> **板子时间是这个项目最容易翻车的地方。**  
> i.MX6ULL 没有 RTC 电池，断电重启后时间从 1970 开始。frpc 与服务端之间开了 TLS，  
> 时间偏差太大握手直接失败，日志里是 `x509: certificate has expired or is not yet valid`  
> 之类，**看起来像 token 错了，其实是时钟错了**。所以上面特意先 `date`。  
> 修：
>
> ```bash
> apt install -y ntpdate && ntpdate -u ntp.aliyun.com
> ```
>
> 或装 `chrony` 长期同步。时钟对了再 `systemctl restart frpc`。
>
> **没有 systemd（Buildroot）**：改用 `/etc/rc.local`，在 `exit 0` 之前加一行  
> `/usr/local/bin/frpc -c /etc/frp/frpc.toml &`。
>
> **板子 IP 会变**：把路由器 DHCP 里按 MAC 做个静态绑定，否则换 IP 后  
> 每次传输都要改 `BOARD=` 的地址。

---

## 第 10 步 · 服务器：换成 443 反代，然后逐层验

```bash
nano /etc/nginx/sites-available/board-site
```

80 段保留（ACME + 跳转），追加 443 段：

```nginx
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name board.nexuslink.icu;

    ssl_certificate     /etc/letsencrypt/live/board.nexuslink.icu/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/board.nexuslink.icu/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;

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

```bash
nginx -t && nginx -s reload
```

> `nginx -t` 若提示 `listen ... http2` 已废弃（Nginx ≥ 1.25.1，24.04 就是），  
> 改成 `listen 443 ssl;` 并在 server 块里加一行 `http2 on;`。

四层验证，从内到外，哪层断了一眼能看出来：

```bash
curl -I http://board.nexuslink.icu            # ① 期望 301
curl -I https://board.nexuslink.icu           # ② 期望 200 OK
ss -lntp | grep 8080                          # ③ 期望 127.0.0.1:8080 已监听
curl -sI https://board.nexuslink.icu | grep -i '^server'   # ④ 期望 server 头被板上 Nginx 抹成 nginx
certbot renew --dry-run                       # ⑤ 期望 all simulated renewals succeeded
```

> ③ 没有 8080 = 板子没连上（回板子看 `journalctl -u frpc`）。  
> ③ 有 8080 但 ② 502 = 板内 Nginx 挂了（回板子 `curl -I 127.0.0.1`）。

---

## 附一 · 板上没有 apt（Buildroot / Yocto rootfs）

先确认到底有没有包管理器：

```bash
which apt opkg apk apt-get 2>/dev/null; cat /etc/os-release 2>/dev/null | head -2; ls /etc/init.d 2>/dev/null | head
```

四条路，按「省事 → 费事」排。**走哪条都不影响后面的服务器部分**——
域名、证书、frps、443 反代跟板上用什么 Web 服务器毫无关系，这正是分层的意义。

### A. 有 rootfs 构建工程 → 把 nginx 编进镜像（最正规）

思路：不往板上装东西，而是让 nginx **成为镜像的一部分**。板子重烧后开箱就带 nginx，
以后所有配置都是「改文件 + reload」，和 Debian 上没区别。

#### A-0 · 前提判定（不满足就别走这条）

```bash
# Buildroot 工程：根目录有 Makefile / .config / output
ls Makefile .config output/images/ 2>/dev/null
# Yocto 工程：根目录有 poky/ 与 build/conf/local.conf
ls poky build/conf/local.conf 2>/dev/null
```

厂商 SDK（正点原子 / 野火 / 米尔）通常给的是「Buildroot 源码包 + 预编译镜像」。
**只给了镜像、没给源码工程，这条路不成立**，直接看 B / C / D。

#### A-1 · 先认配置，千万别覆盖它

```bash
cd <SDK目录>/buildroot
ls output/images/          # 应能看到 rootfs.ext4 / rootfs.tar / zImage 之类
grep -E 'BR2_cortex|BR2_TOOLCHAIN_BUILDROOT_(GLIBC|UCLIBC|MUSL)|BR2_GCC_TARGET_FLOAT_ABI' .config
```

期望：`BR2_cortex_a7=y`、`BR2_TOOLCHAIN_BUILDROOT_GLIBC=y`（或 UCLIBC）、`BR2_GCC_TARGET_FLOAT_ABI="hard"`。

> **不要在这里跑 `make imx6ull_defconfig`。** 它会把当前镜像对应的 `.config` 冲掉，
> 编出来的二进制可能和你板上那套 libc 不是同一套 —— 表现是拷过去运行直接
> `not found` 或 `No such file or directory`（找不到动态链接器，不是文件真的不存在）。
> 已有 `.config` 就沿用它。

#### A-2 · 勾包

```bash
make menuconfig
```

`Target packages` → `Networking applications` → `[*] nginx`，进去细调：

| 选项 | 勾不勾 | 理由 |
| --- | --- | --- |
| `http_gzip_static` | **勾** | 配合第 1 步的 `.gz`，A7 不用实时压缩 |
| `http_ssl` | **不勾** | TLS 在服务器终结，板上只跑明文 80；勾了会拖进 openssl，多占 1 MB flash |
| `pcre` | **勾** | 配置里有正则 `location ~ /\.(?!well-known)`，没 PCRE 会 `nginx -t` 报错 |
| `dav / flv / xslt / perl / mail / stream` | **全不勾** | 纯占体积 |
| `http_stub_status` | 可选 | 想让 `/status` 页面显示连接数再勾 |

> 条目名对不上就在 menuconfig 里按 `/` 搜 `NGINX`，能列出这一版真实的全部选项。
> 不同 Buildroot 版本略有增减，别照抄我表里的字面。

`Exit` 一路保存。想留个可复现的配置：`cp .config ../my-nginx.config`。

#### A-3 · 只编 nginx，不用全量

```bash
make nginx 2>&1 | tail -30
```

只编 nginx 及其依赖（zlib / pcre），一般一两分钟。全量 `make` 几十分钟，**编译期不需要**。

#### A-4 · 直接取产物，不必重烧（这一步省掉 90% 的时间）

```bash
ls -l output/target/usr/sbin/nginx
ls output/target/etc/nginx/
file output/target/usr/sbin/nginx
```

`file` 应显示 `ELF 32-bit LSB executable, ARM, EABI5` —— 这就是和板上同一套 libc 编出来的二进制。
`output/target/` 是暂存的完整 rootfs，nginx 的可执行文件、`nginx.conf`、`mime.types` 都在里面。

```bash
tar -C output/target -czf - usr/sbin/nginx etc/nginx | ssh root@192.168.1.50 "tar -xzf - -C /"
```

> rootfs 是只读的（squashfs，或挂成 ro 的 ext4）时这一步会 `Read-only file system`。
> 改传 `/usr/local`：
> ```bash
> tar -C output/target -czf - usr/sbin/nginx etc/nginx | ssh root@192.168.1.50 "tar -xzf - -C /usr/local --strip-components=1"
> ```
> 之后所有命令都显式带路径：`/usr/local/sbin/nginx -c /etc/nginx/nginx.conf`。

#### A-5 · 板上跑起来（没有 systemd）

```bash
mkdir -p /var/log/nginx /var/lib/nginx/tmp
nginx -t
nginx
curl -I http://127.0.0.1/
```

三个必遇到的启动错误：

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `getpwnam("nginx") failed` | `nginx.conf` 里写了 `user nginx;`，rootfs 没这个用户 | 改 `user root;`，或 `adduser -S -H nginx` |
| `open() "/var/log/nginx/error.log" failed` | 目录不存在 | 上面那句 `mkdir` |
| `open() "/etc/nginx/mime.types" failed` | 只拷了二进制没拷配置 | 把 `output/target/etc/nginx/` 整个拷过去 |

然后接第 4 步：把那份 `site.conf` 放进 `/etc/nginx/conf.d/site.conf`，并在 `nginx.conf` 的
`http { }` 里补一行

```nginx
include /etc/nginx/conf.d/*.conf;
```

> Buildroot 的默认 `nginx.conf` 没有这行 include，你放好 `site.conf` 也不会生效 ——
> 现象是访问 80 端口仍返回 Buildroot 默认页，很容易以为是配置写错了。

开机自启：Buildroot 自带 `/etc/init.d/S50nginx`，`chmod +x` 后重启即生效；
没有这个脚本就在 `/etc/rc.local` 里 `exit 0` 之前加一行 `nginx`。

#### A-6 · 固化进镜像（交作业 / 量产才需要）

把「站点文件 + 站点配置」变成镜像的一部分，重烧后开箱即用：

```bash
mkdir -p board/imx6ull/rootfs-overlay/var/www/site
cp -r dist/* board/imx6ull/rootfs-overlay/var/www/site/
mkdir -p board/imx6ull/rootfs-overlay/etc/nginx/conf.d
cp deploy/nginx.conf board/imx6ull/rootfs-overlay/etc/nginx/conf.d/site.conf
```

`make menuconfig` → `System configuration` → `Root filesystem overlay directories` 填
`board/imx6ull/rootfs-overlay`，然后：

```bash
make savedefconfig BR2_DEFCONFIG=configs/imx6ull_defconfig   # 改动写回 defconfig，可复现
make                                                          # 全量出镜像
```

> **重烧 rootfs 会把板上现存的 `/var/www/site` 和 `/etc/frp/frpc.toml` 一起冲掉。**
> 要么把 `frpc.toml` 也放进 overlay（注意里面是 token，别提交到公开仓库），
> 要么重烧完按第 9 步重新配一遍。

只更新 rootfs 分区，三种介质三种命令：

| 介质 | 做法 |
| --- | --- |
| SD 卡 | `dd if=output/images/rootfs.ext4 of=/dev/sdX2 bs=4M status=progress`（**`of=` 写错就是抹掉整块盘**） |
| eMMC | u-boot 里 `ums` / `fastboot` 把板子挂成 U 盘后 dd，或用厂商 mfgtools 只勾 rootfs 分区 |
| NAND (ubifs) | `ubiformat /dev/mtd3 -f rootfs.ubifs`，再 `ubiattach` / `ubimkvol` 挂回去 |

#### A-7 · Yocto 的等价操作（只列差异）

```bash
bitbake-layers show-layers | grep meta-webserver     # 没输出说明层没加
bitbake-layers add-layer ../meta-openembedded/meta-webserver
```

`conf/local.conf`：

```
PACKAGECONFIG:append:pn-nginx = " http-gzip-static"
IMAGE_INSTALL:append = " nginx"
```

```bash
bitbake -e nginx | grep -E '^PACKAGECONFIG='   # 先看这版 recipe 的真实开关名
bitbake nginx
bitbake core-image-minimal                     # 换成你自己的镜像名
```

产物在 `tmp/work/<machine>/nginx/<ver>-r0/image/usr/sbin/nginx`，拷法同 A-4；
整包镜像在 `tmp/deploy/images/<machine>/`（`.tar.bz2` / `.wic`）。

**代价**：一次编译（Buildroot 增量 1~2 分钟，Yocto 十几分钟起），换来板上永久有 nginx、
`/etc/nginx` 结构和 Debian 一致、第 4 步配置原样可用，而且**换机器/重烧不用重来**。
这是四条路里唯一「一次投入、长期省事」的。

### B. 不想编镜像 → 让 frpc 自己当 Web 服务器（最省事）

frp 带 `static_file` 插件：**板上不需要 Nginx，只要那一个静态 frpc 二进制**。
第 5 步把文件传到 `/var/www/site` 照旧，只把 `/etc/frp/frpc.toml` 的 proxies 段换成：

```toml
[[proxies]]
name = "board-web"
type = "tcp"
remotePort = 8080

[proxies.plugin]
type = "static_file"
localPath = "/var/www/site"
stripPrefix = ""
```

```bash
frpc -c /etc/frp/frpc.toml
```

要接受的代价：

- 没有 `gzip_static`，第 1 步的 `.gz` 白压了，按原文发送（单页 8–40 KB，还行）
- 没有 `expires` / `Cache-Control`，静态资源不被浏览器缓存
- 没有 `server_tokens off`，响应头署名是 frp
- 站内 `/status` 页面里 "nginx worker rss" 会是空的——那本来就依赖第 4 步的采集，得自己补

好处是内存更省：整机只多一个 frpc 进程，静态文件由它直接发。

### C. busybox 自带 httpd → 零下载

```bash
busybox --list | grep -E '^(httpd|inetd)$'
mkdir -p /var/www/site
busybox httpd -p 80 -h /var/www/site
```

开机自启（没有 systemd 时）写 `/etc/rc.local` 的 `exit 0` 之前：

```sh
busybox httpd -p 80 -h /var/www/site
```

代价：按连接 fork、无 gzip、无缓存头，几个并发就慢。课程演示够用，
而且 `deploy/nginx.conf` 里那些 `sendfile` / `keepalive` 调优就没得调了。

### D. 自己交叉编译 nginx（最后手段）

前提：板子的交叉工具链（正点原子 / 野火 SDK 里那个 `arm-linux-gnueabihf-gcc`）。

```bash
wget https://nginx.org/download/nginx-1.24.0.tar.gz
tar xzf nginx-1.24.0.tar.gz && cd nginx-1.24.0
./configure --prefix=/usr/local/nginx \
  --with-cc=arm-linux-gnueabihf-gcc --with-cpp=arm-linux-gnueabihf-g++ \
  --without-http_rewrite_module --without-http_gzip_module --without-http_ssl_module
make -j4 && make install DESTDIR=$PWD/out
```

传上去（板上有 dropbear / ssh 时）：

```bash
tar -C out -czf - . | ssh root@192.168.1.50 "tar -xzf - -C /"
```

> 板子上连 ssh 都没有（只有串口）时，三条备选：busybox `tftp` 拉取、
> u-boot 里 `tftp` 到内存再写 eMMC/SD、或者最稳的——拷到 SD 卡，板子挂载后 `cp`。

**三个必踩的坑：**

1. `--without-http_rewrite_module` 是为了绕开 PCRE 交叉编译，代价是**没有正则 location**。
   `deploy/nginx.conf` 里 `location ~ /\.(?!well-known)` 那一块会让 `nginx -t` 报错，删掉。
2. `--without-http_gzip_module` 绕开 zlib，于是 `gzip_static on;` 也没了，同样删掉，纯原文传输。
3. `/usr/local/nginx/logs/` 是 `make install` 时创建的。只拷二进制过去的话先
   `mkdir -p /usr/local/nginx/logs`，否则启动就报
   `open() "/usr/local/nginx/logs/nginx.pid" failed`。

---

## 日常更新（每次改完内容，仍是手操三条）

```bash
cd /d/Documents/大三上/嵌入式系统/项目/embeded_web
npx astro build
cd dist && find . -type f \( -name '*.html' -o -name '*.css' -o -name '*.xml' -o -name '*.json' -o -name '*.svg' \) -exec gzip -9 -k -f {} \; && cd ..
tar -C dist -czf - . | ssh root@192.168.1.50 "rm -rf /var/www/site/* && tar -xzf - -C /var/www/site && nginx -s reload"
```

> **不要在板子上 `npm install` / `npm run build`**。Astro 构建峰值内存超过 500 MB，  
> 512 MB 的板子必然 OOM，轻则构建失败，重则把 sshd 一起 OOM 掉、直接失联。
>
> 走「附一」B / C（板上没有 Nginx）时，把最后那条命令末尾的 `&& nginx -s reload` 删掉：
> `static_file` 插件和 busybox httpd 每次请求都直接读磁盘，换完文件就生效。

---

## 故障速查


| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 浏览器 502 | 隧道断 / 板上 Nginx 没起 | 板上 `curl -I 127.0.0.1`；`journalctl -u frpc` |
| frpc 日志 `login to server failed` | 两边 token 不一致 | 对比 `frps.toml` 与 `frpc.toml` |
| frpc 日志 x509 / certificate | 板子时间停在 1970 | `date` → `ntpdate -u ntp.aliyun.com` |
| 板子连不上服务器 | 云安全组没放 7000 | 控制台加规则；`telnet 112.124.58.134 7000` |
| certbot 报 `DNS problem` | A 记录没生效 | `nslookup board.nexuslink.icu 223.5.5.5` |
| certbot 报 404 | ACME location 的 root 与 `-w` 不一致 | 手建测试文件再 curl |
| Nginx 起不来 `duplicate default server` | `sites-enabled/default` 还在 | 删软链后 reload |
| `unknown directive gzip_static` | 缺模块 | 装 `libnginx-mod-http-gzip-static` 或改 `gzip on` |
| 外网能直连 `:8080` | `proxyBindAddr` 忘了写 | 必须 `127.0.0.1`，改完 `systemctl restart frps` |

常用日志：服务器 `journalctl -u frps -f`、`tail -f /var/log/nginx/error.log`；
板子 `journalctl -u frpc -f`、`journalctl -u nginx -f`。

---

## 附二 · 实际部署记录（2026-11-08，已上线）

上面是通用流程，这一节是**这台机器上真跑起来的版本**，包含所有临时绕路和踩坑。

### 实际拓扑

```
浏览器
  │  HTTPS 443（Let's Encrypt 证书在这里终结）
  ▼
阿里云 ECS 112.124.58.134 · nginx 1.24
  │  proxy_pass 127.0.0.1:8080（frps 绑本机，公网碰不到）
  ▼
frps 0.58.1 · bindPort 7000
  │  ← frpc 主动外连（板子在 NAT 后面，无需公网 IP）
  ▼
i.MX6ULL（ATK-IMX6U，kernel 4.1.15，Yocto + sysvinit，494MB）
  │  frpc 的 static_file 插件直接发文件
  ▼
/var/www/site（29 个文件 / 556 KB）
```

板子上**没有**用 nginx：走的是方案 B，`static_file` 插件直接由 frpc 发静态文件。

### 落地清单

| 位置 | 内容 |
| --- | --- |
| 服务器 `/etc/frp/frps.toml` | `bindPort 7000`、`proxyBindAddr 127.0.0.1`、token、`allowPorts 8080-8089` |
| 服务器 `/etc/systemd/system/frps.service` | 常驻 + `Restart=always` |
| 服务器 `/etc/nginx/sites-available/board` | 80 跳转 + 443 反代 8080 |
| 服务器 `/etc/letsencrypt/live/board.nexuslink.icu/` | 证书，到期 2027-02-06，自动续期已验 |
| 板子 `/usr/local/bin/frpc` | 0.58.1，arm 静态二进制，13MB |
| 板子 `/etc/frp/frpc.toml` | 见 `deploy/frpc-static.toml` |
| 板子 `/etc/init.d/frpc` + `/etc/rc5.d/S99frpc` | 开机自启（见 `deploy/frpc.init`） |
| 板子 `/var/www/site/` | 站点产物 |

### 三个只有真机才会暴露的坑

**① 板上 SSH 用的是老 dropbear，现代 OpenSSH 直接拒连**

报错 `no matching host key type found. Their offer: ssh-rsa`。加两个参数即可：

```bash
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.137.112
scp 同样要带这两个参数
```

不解决这个就只能挤串口敲命令，效率差一个数量级。

**② frpc 开机启动比网络就绪还早 → 登录失败一次就退出**

`rc5.d/S99frpc` 执行时 eth0 还没拿到 IP，日志是
`dial tcp ...: connect: network is unreachable`，然后
`With loginFailExit enabled, no additional retries will be attempted` —— 进程直接退了。
**现象是每次重启站点都 502，手动 `/etc/init.d/frpc start` 又立刻好**。

`frpc.toml` 里加一行即可，比在 init 脚本里 sleep 等待更可靠：

```toml
loginFailExit = false
```

**③ `pkill -f "frpc -c"` 会把自己那条 ssh 命令一起杀掉**

`ssh root@板子 "pkill -f 'frpc -c'"` 里，远端 shell 的命令行本身也包含 `frpc -c`，
`pkill -f` 按完整命令行匹配，于是杀掉了自己 → ssh 返回空输出、退出码 0，看起来像"命令没执行"。
改用 `pkill -x frpc`（按进程名精确匹配），或用 init 脚本的 `stop`。

### 日常更新（方案 B 版）

```bash
cd /d/Documents/大三上/嵌入式系统/项目/embeded_web
npx astro build
tar czf /tmp/site.tar.gz -C dist .
scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    /tmp/site.tar.gz root@192.168.137.112:/tmp/
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.137.112 \
    'rm -rf /var/www/site/* && tar xzf /tmp/site.tar.gz -C /var/www/site'
```

不用重启 frpc，也不用 reload——`static_file` 每次请求都直接读磁盘。
不需要预压缩 `.gz`（frp 插件没有 `gzip_static`）。

### 方案 B 的已知取舍

- 无 gzip：页面按原文传（首页 27KB，压缩后会小一半）
- 无 `Cache-Control`，`_astro/*.css` 不被浏览器缓存
- 404 由 frp 返回纯文本 `404 page not found`，**不用**站点的 `404.html`
- 响应头不隐藏服务信息

这些在板上已有 nginx 的前提下都能补回来：把 `/etc/frp/frpc.toml` 换成 `deploy/frpc.toml`
（`localIP 127.0.0.1` / `localPort 80`），再把 `deploy/nginx.conf` 放到 `/etc/nginx/conf.d/site.conf`
即可 —— 板子的 `rc5.d/S92nginx` 本来就已经让 nginx 开机自启了。

