---
title: '在 512MB 内存的小机器上跑网站的省内存实践'
description: '从系统参数到 Web 服务器配置，一份针对小内存 VPS 的完整清单。'
pubDate: 2026-10-20
tags: ['Linux', '建站', '运维']
---

小内存机器不是缺陷，是一种约束美学。512MB 跑一个博客绰绰有余，关键是每一层都要选对。

## 原则：能静态就静态

动态语言运行时（PHP-FPM、Node 常驻进程）是内存大户。静态博客只需：

| 方案 | 常驻内存 | 说明 |
| --- | --- | --- |
| 纯静态 + Nginx | ~15MB | 本文方案 |
| WordPress + PHP-FPM | 250MB+ | 512MB 机器上很勉强 |
| Ghost / Node 常驻 | 150MB+ | 可以但没必要 |

## Nginx 小内存配置要点

```nginx
# 按核心数而非 auto，512MB 机器通常 1 核
worker_processes 1;
events { worker_connections 512; }

# 关闭不需要的模块，压缩文本
gzip on;
gzip_comp_level 5;
gzip_types text/css application/xml application/rss+xml;
```

## 系统层

```bash
# 看看谁在吃内存
free -m
ps aux --sort=-%mem | head -5
```

- 卸掉 `snapd`（一开机就吃 100MB+）
- 用 `dropbear` 替代 `openssh-server` 能再省一点
- 加 1GB swap 兜底，防止构建时 OOM

## 小结

约束催生简单。这个站连同系统开销，整机能稳定控制在 100MB 以内。
