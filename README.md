# 轻痕 · Light Marks —— 轻量化个人博客

一个为 **512MB 内存小机器**设计的纯静态个人博客。

## 特性

- **纯静态输出**：Astro 构建，页面零 JavaScript，整站仅 HTML + CSS
- **服务器极轻**：只需一个 Nginx（常驻内存约 15MB），无数据库、无后端进程
- **Markdown 写作**：文章即文件，Git 天然管理版本，`draft: true` 即草稿
- **完整功能**：首页 / 文章页 / 标签云 / 按年归档 / RSS / sitemap / SEO meta

## 快速开始

```bash
npm install
npm run dev       # 本地预览 http://localhost:4321
```

### 写一篇文章

在 `src/content/blog/` 新建 `.md` 文件：

```markdown
---
title: '文章标题'
description: '一句话摘要'
pubDate: 2026-10-22
tags: ['随笔']
draft: false      # true 则不发布
---

正文……
```

## 部署到 512MB Linux 服务器

**第 1 步（服务器上，只做一次）**：安装 Nginx 并挂上站点配置

```bash
scp -r deploy/ user@your-server:~/blog-deploy
ssh user@your-server
sudo bash ~/blog-deploy/server-init.sh   # 安装 nginx + 配置
```

**第 2 步（本机，每次发布）**：

```bash
# 先改 deploy/deploy.sh 里的 SERVER 变量
npm run deploy    # 构建 + rsync 增量上传
```

**建议开启 HTTPS**：

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d 你的域名
```

## 512MB 服务器优化备忘

- 本方案只跑 Nginx，整机常驻内存可控制在 100MB 内
- `worker_processes 1`、`worker_connections 512` 已按单核小机调好（见 `deploy/nginx.conf`）
- 若想在服务器上直接构建（不推荐），请加 1GB swap 防 OOM；正常流程是本地构建、只上传 `dist/`
- 卸载 `snapd` 可再释放 100MB+ 内存

## 目录结构

```
├── astro.config.mjs        # 站点域名等配置
├── src/
│   ├── content/blog/       # ← 你的文章（Markdown）
│   ├── content.config.ts   # frontmatter 校验规则
│   ├── layouts/            # 页面骨架
│   ├── components/         # Header / Footer / 文章卡片
│   ├── pages/              # 首页、归档、标签、RSS
│   └── styles/global.css   # 全部样式（一个文件）
├── deploy/
│   ├── nginx.conf          # 服务器 Nginx 配置
│   ├── server-init.sh      # 服务器初始化脚本
│   └── deploy.sh           # 一键构建 + 上传
└── public/favicon.svg
```

## 修改站点名

全局搜索「轻痕」替换成你喜欢的名字即可（`src/layouts/Base.astro`、`src/components/Header.astro`、`src/pages/rss.xml.js`）。
