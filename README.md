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
| `/status/` | 运行状态说明与采集脚本 |
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

完整逐条操作见 **[deploy/SERVER-SETUP.md](deploy/SERVER-SETUP.md)**，含每步的验证命令与故障对照表。
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

```bash
# 安装 nginx 与 frpc，然后：
sudo cp deploy/nginx.conf /etc/nginx/conf.d/site.conf
sudo cp deploy/frpc.toml /etc/frp/frpc.toml     # 改 serverAddr 与 token
sudo mkdir -p /var/www/site
sudo systemctl enable --now nginx frpc

# 状态采集（可选，用于首页状态面板）
sudo install -m 755 deploy/status-collector.sh /usr/local/bin/
sudo systemctl enable --now status-collector.timer
```

**第 3 步 · 每次发布（本机）**

```bash
BOARD=root@192.168.1.50 npm run deploy
```

脚本做四件事：构建 → 预压缩 `.gz` → rsync 到板子 → 重载 Nginx。
**不要在开发板上执行 `npm run build`**——Node 在 armhf 上构建设站峰值内存超过 500MB，512MB 的板子必然 OOM。

## 目录结构

```
.
├── astro.config.mjs          站点配置（域名 / 静态输出）
├── deploy/
│   ├── deploy.sh             构建 + 预压缩 + rsync + reload
│   ├── nginx.conf            板上 Nginx 站点配置
│   ├── status-collector.sh   生成 status.json 的采集脚本
│   ├── frpc.toml             开发板 frp 客户端
│   ├── frps.toml             公网服务器 frp 服务端
│   └── SERVER-SETUP.md       公网服务器手动初始化完整步骤
├── public/
│   ├── favicon.svg
│   └── status.json           状态快照（板上定时覆盖）
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
| 框架 JS | 0 KB | 0 KB（仅一段 0.4KB 内联状态脚本） |
| Nginx 常驻内存 | < 10 MB | 3.2 MB |
| 局域网 TTFB | < 50 ms | 3.2 ms |
| 公网可访问 | 域名 + HTTPS | 已上线 |

详细测量方法见站内《性能实测：同一个页面在 x86 与 i.MX6ULL 上》。

## 说明

`deploy/` 下的配置中域名、IP、token 均为占位值，部署前替换成自己的。
