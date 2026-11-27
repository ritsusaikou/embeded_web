# 嵌入式系统课程项目站 · i.MX6ULL

一个跑在 **i.MX6ULL 开发板（512MB 内存 / 单核 Cortex-A7）** 上的静态站点，
经 **frp 反向隧道** 发布到公网域名。站点本身是课程项目的交付物之一。

- 页面零前端框架 JS，全站只有一份 CSS
- 板上只跑一个 Nginx worker（常驻约 3.2MB）+ frpc
- 8 篇实验日志、4 篇技术选型分析全部为 Markdown 内容

## 站点板块

| 路径 | 内容 |
| --- | --- |
| `/` | 首页 Dashboard：运行状态、指标、板块导航、实验时间线 |
| `/lab/` | 实验日志（8 篇，含列表与详情） |
| `/project/` | 课程项目：架构图、部署流程、关键配置、实测数据 |
| `/course-design/` | 课程设计（Qt 应用）· 内容待上传 |
| `/docs/` | 系统设计文档：分层、引脚表、工具链、构建烧录、目录结构 |
| `/notes/` | 技术选型：静态优先、frp 落地、Astro 原理、性能实测 |
| `/source/` | 源码与产物：仓库结构、脚本说明、产物清单 |
| `/gallery/` | 演示素材 · 待补充 |
| `/console/` | 远程控制台：WebSocket 终端 + 屏幕实时镜像 + 指令级遥控（起应用/回桌面，需 token） |
| `/status/` | 运行状态：字段、来源、为何用常驻 agent |
| `/about/` | 关于本站 |

## 快速开始

```bash
npm install
npm run dev      # http://localhost:4321
npm run build    # 产物在 dist/
npm run preview  # 预览构建产物
```

## 部署到开发板

**第 1 步 · 公网服务器（只做一次，纯手动）**

完整逐条操作见 **[deploy/DEPLOY.md](deploy/DEPLOY.md)**，含两种 Web 服务方案的原理、
实现与故障对照表。
流程概要：

```bash
# ① 装 Nginx 与 certbot
sudo apt update && sudo apt install -y nginx certbot

# ② 建 ACME 校验目录
sudo mkdir -p /var/www/letsencrypt

# ③ 写 /etc/nginx/sites-available/board-site（80 端口 + acme location），启用并 reload

# ④ 申请证书（webroot 方式，不改 Nginx 配置）
sudo certbot certonly --webroot -w /var/www/letsencrypt \
  -d 你的域名 -m 你的邮箱 --agree-tos --no-eff-email -n

# ⑤ 装 frps，写 /etc/frp/frps.toml，用手写的 systemd 单元常驻
sudo systemctl enable --now frps

# ⑥ 把站点改成 443 + proxy_pass 127.0.0.1:8080，nginx -t && reload
```

**第 2 步 · 开发板（只做一次）**

板子是 Yocto + **sysvinit**，没有 systemd，所以不能 `systemctl enable`。
自启靠 `/etc/init.d/` + `/etc/rc5.d/S*` 软链，进程守护靠 cron 巡检（详见 `deploy/DEPLOY.md` 第 13 节）：

```bash
# ① 站点配置：放 /etc/nginx/conf.d/site.conf
#    Yocto 默认的 nginx.conf 没有 include conf.d/*.conf，要先补这一行
cp deploy/nginx.conf /etc/nginx/conf.d/site.conf
/usr/sbin/nginx -t && /usr/sbin/nginx -s reload

# ② frpc：二进制 + 配置 + 开机自启
cp deploy/frpc.toml /etc/frp/frpc.toml          # 改 serverAddr 与 token
install -m 755 deploy/frpc.init /etc/init.d/frpc
ln -sf /etc/init.d/frpc /etc/rc5.d/S99frpc
ln -sf /etc/init.d/frpc /etc/rc0.d/K01frpc
ln -sf /etc/init.d/frpc /etc/rc6.d/K01frpc
/etc/init.d/frpc start

# ③ 进程守护：frpc/nginx 挂了自动拉起，兼开机校时
install -m 755 deploy/keepalive.sh /usr/local/bin/board-keepalive.sh
(crontab -l 2>/dev/null; echo "* * * * * /usr/local/bin/board-keepalive.sh") | crontab -

# ④ 固定 MAC：不做这步，下面配的静态 IP 每次重启都会失效
#    设备树 local-mac-address 全 0，内核每次开机随机生成 MAC，
#    而 connman 服务名是 ethernet_<MAC>_cable，MAC 一变静态配置就对不上
install -m 755 deploy/set-mac.init /etc/init.d/set-mac
ln -sf ../init.d/set-mac /etc/rc5.d/S04set-mac   # S04 必须早于 S05connman

# ⑤ 静态 IP（ICS 的 DHCP 不支持 MAC 绑定，只能板子侧固定）
connmanctl config ethernet_886044c24616_cable \
  --ipv4 manual 192.168.137.91 255.255.255.0 192.168.137.1
connmanctl config ethernet_886044c24616_cable --nameservers 192.168.137.1

# ⑥ 状态采集（可选，用于首页状态面板）
install -m 755 deploy/status-collector.sh /usr/local/bin/
```

> 日志都在 `/var/lib/frpc/`（rootfs）。**不要**指望 `/var/log` —— 它是指向
> `/var/volatile/log` 的软链，tmpfs，断电即丢。

**第 2.5 步 · 板端 agent（第 17 节，可选）**

首页的实时数字、`/console/` 的终端与屏幕镜像，都靠板上一个常驻的
`board-agent.py`（纯标准库，无第三方依赖）：

```bash
scp deploy/board-agent.py deploy/board-agent.init root@192.168.137.91:/tmp/
ssh root@192.168.137.91 '
  install -m 755 /tmp/board-agent.py /usr/local/bin/board-agent.py
  install -m 755 /tmp/board-agent.init /etc/init.d/board-agent
  ln -sf /etc/init.d/board-agent /etc/rc5.d/S98board-agent
  ln -sf /etc/init.d/board-agent /etc/rc0.d/K02board-agent
  ln -sf /etc/init.d/board-agent /etc/rc6.d/K02board-agent
  /etc/init.d/board-agent restart'
cat /etc/board-agent.token          # 浏览器里连接要用的令牌
```

板上 nginx 加 `/agent/api/`、`/agent/ws/` 两个 location，服务器 443 再加一个
`/agent/ws/` 透传 `Upgrade` —— 细节和排错表见 `deploy/DEPLOY.md` 第 17 节。

> 想让板上完全不依赖 nginx，可改用 `deploy/frpc-static.toml`（frpc 自带静态文件服务），
> 代价是没有 gzip、缓存头和自定义 404。两种方案的对比与互切见 `DEPLOY.md` 第 1、5 节。

**第 3 步 · 每次发布（本机）**

本机没有 rsync（Windows）时用三条命令，详见 `DEPLOY.md` 第 6 节：

```bash
npx astro build                                  # 构建
tar czf site.tar.gz -C dist .                    # 打包（方案 A 需先预压缩 .gz）
scp site.tar.gz root@<板子IP>:/tmp/              # 上传后板上解压
```

有 rsync 的机器可以用脚本一步完成：

```bash
BOARD=root@<板子IP> npm run deploy
```

脚本做四件事：构建 → 预压缩 `.gz` → rsync 到板子 → 重载 Nginx。
**不要在开发板上执行 `npm run build`**——Node 在 armhf 上构建设站峰值内存超过 500MB，512MB 的板子必然 OOM。

## 目录结构

```
.
├── astro.config.mjs          站点配置（域名 / 静态输出）
├── deploy/
│   ├── DEPLOY.md             部署文档：两种方案、原理、实现、排错
│   ├── deploy.sh             构建 + 预压缩 + rsync + reload（需 rsync）
│   ├── nginx.conf            板上 Nginx 站点配置（方案 A，含 /agent/ 反代）
│   ├── nginx-server.conf     公网服务器站点配置（443 + Upgrade 透传）
│   ├── board-agent.py        板端 agent：状态 API / PTY shell / framebuffer 推流 / GUI 遥控
│   ├── board-agent.init      agent 的 sysvinit 脚本（rc5.d/S98 + rc0/rc6.d/K02）
│   ├── board-time.sh         HTTP Date 校时（板上没有 ntpdate，原 ntpdate-sync 是空转的）
│   └── touch/                触摸注入实验：LD_PRELOAD 侧门 + tslib 模块（见 DEPLOY.md 第 18 节）
│   ├── status-collector.sh   生成 status.json 的采集脚本
│   ├── frpc.toml             开发板 frp 客户端（转发本机 80，方案 A）
│   ├── frpc-static.toml      开发板 frp 客户端（静态插件，方案 B）
│   ├── frpc.init             板上 frpc 的 init 脚本（sysvinit，S99）
│   ├── keepalive.sh          进程守护（cron 每分钟巡检，兼开机校时）
│   ├── set-mac.init          固定 eth0/eth1 MAC（须早于 connman）
│   ├── install-board.sh      一键安装（自包含，内含上面三个脚本，幂等）
│   └── frps.toml             公网服务器 frp 服务端
├── public/
│   ├── favicon.svg
│   └── status.json           状态快照（构建时兜底，agent 不可达时用）
└── src/
    ├── content/
    │   ├── lab/              8 篇实验日志（Markdown）
    │   └── note/             4 篇技术选型分析
    ├── components/           Header / Footer / Icon / SectionHead / ArchDiagram
    ├── data/site.ts          站名、导航、指标、状态快照
    ├── layouts/Base.astro
    ├── pages/                各板块页面
    └── styles/global.css     全站唯一样式表
```

## 维护内容

**加一篇实验日志**：在 `src/content/lab/` 新建 `.md`，frontmatter 必填
`title / order / week / date / summary`，可选 `tags / duration / hardware / status`。
列表、时间线、RSS、上一篇/下一篇会自动更新。

**加一篇技术分析**：放在 `src/content/note/`，必填 `title / date / summary`，
可选 `order / tags / reading`。

**改站名或导航**：只改 `src/data/site.ts`。

## 设计目标与实测

| 目标 | 设定 | 实测 |
| --- | --- | --- |
| 单页体积 | < 100 KB | 约 8 KB（gzip） |
| 框架 JS | 0 KB | 0 KB（只有原生脚本；控制台页的 xterm.js 约 90KB gzip，单独一页） |
| Nginx 常驻内存 | < 10 MB | 3.2 MB |
| 局域网 TTFB | < 50 ms | 3.2 ms |
| 公网可访问 | 域名 + HTTPS | 已上线 |

详细测量方法见站内《性能实测：同一个页面在 x86 与 i.MX6ULL 上》。

## 说明

`deploy/` 下的配置中域名、IP、token 均为占位值，部署前替换成自己的。
