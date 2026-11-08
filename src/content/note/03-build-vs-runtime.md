---
title: Astro 与 Vue / React 到底差在哪
order: 3
date: 2026-11-06
summary: 一个是「把渲染放在构建时」，一个是「把渲染放在浏览器运行时」。顺带解释为什么两者都能 npm run dev，以及 dev server 与构建产物的本质区别。
tags: [Astro, 前端框架, 构建原理]
reading: 6 分钟
---

## 一句话区别

**Vue / React 把渲染放在浏览器运行时，Astro 把渲染放在构建时。**

不是因为 Astro「更先进」，而是它们解决的问题不同：前者要构建**交互式应用**，后者要构建**内容型页面**。

## 两种产物

| | Vue / React | Astro |
| --- | --- | --- |
| 你写的东西 | `.vue` / `.jsx` 组件 | `.astro` 组件 + Markdown |
| 构建产物 | HTML 骨架 + 一个 JS bundle | 完整 HTML + CSS |
| 页面何时成形 | 浏览器执行完 JS 之后 | 构建完成那一刻 |
| 首屏 | 依赖 JS，失败即白屏 | HTML 直出，无 JS 也有内容 |
| 默认 JS 体积 | 框架运行时 + 业务代码，几十到几百 KB | 0 KB |

一个常见的误解是「Astro 也是前端框架，所以它也会打包 JS」。实际上去看产物就清楚了：这个项目的 `dist/` 里只有 `.html`、一个 `.css`，**没有任何 `.js`**。

## Astro 并不排斥 Vue / React

它可以按需引入：`npx astro add react`，然后在 `.astro` 里写 `<Counter client:load />`。这引出 **岛屿架构（Islands Architecture）**：

- 整页是静态 HTML——「海面」；
- 只有需要交互的组件是「岛屿」，只有这些岛屿的 JS 会因为 `client:*` 指令被下载并在浏览器 hydrate；
- 没写 `client:*` 的组件，其 JS **根本不会进产物**。

所以「要不要用框架」从「全局选边」变成了「按组件决定」。对一个博客来说，可能只有搜索框需要 JS。

## 为什么两者都能 `npm run dev`

因为 `npm run dev` 跟框架没关系，它只是 npm 的通用机制：读 `package.json` 的 `scripts.dev`，执行那条命令。而这些工具都满足同一个模式——**以 npm 包形式分发，包内带一个 CLI 可执行文件，CLI 内置基于 Node 的开发服务器**（HTTP 服务 + 文件监听 + WebSocket 推送热更新）。

更巧的是底层是同一个东西：**Astro 内部用 Vite 驱动**，Vue / React + Vite 项目也是 Vite。所以启动方式、端口风格、热更新速度都一样——因为跑的是同一个工具。

但 dev server 干的活不同：

| | Vue / React + Vite | Astro dev |
| --- | --- | --- |
| 收到请求时 | 把源码按需转成 ESM 模块发给浏览器 | 在 Node 里**当场渲染**成完整 HTML 返回 |
| DOM 从哪来 | 浏览器执行 JS 生成 | 服务器已经渲染好 |
| 生产环境 | 仍是浏览器跑 JS | 产物是静态文件，**不需要 Node** |

最后一行是关键：`astro dev` 有 Node 进程，但那只是开发工具。`astro build` 之后就不需要 Node 了。很多人误以为「Astro 站必须用 Node 部署」，就是把这两个阶段混为一谈。

## 顺带一句：dev 和 build 产物差别很大

`astro dev` 不压缩、带 sourcemap、按需渲染，启动就要两百多 MB 内存。**在 512MB 的开发板上绝对不能跑 dev server**，构建必须在 PC 上完成，只把 `dist/` 传上去。

## 判断标准

| 需求 | 选择 |
| --- | --- |
| 博客、文档、产品介绍、课程项目展示 | Astro（静态输出） |
| 后台管理、在线编辑器、复杂状态流转 | Vue / React SPA |
| 内容为主 + 少量交互 | Astro + 少量岛屿 |
| 需要登录、数据库、实时协作 | 前端框架 + SSR 框架（Next / Nuxt） |

对本项目来说，答案很清楚：内容全是 Markdown，交互需求几乎为零，**用 Vue 或 React 属于给自己加一层运行时负担**。
