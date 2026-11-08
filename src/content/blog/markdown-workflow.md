---
title: 'Markdown 写作的极简工作流'
description: '编辑器、Git、一次构建：写文章不需要更复杂的流程。'
pubDate: 2026-10-21
tags: ['Markdown', '建站']
---

## 三步走

1. 在 `src/content/blog/` 下新建 `.md` 文件
2. 写好 frontmatter（标题、日期、标签）
3. 提交并部署

```markdown
---
title: '文章标题'
pubDate: 2026-10-21
tags: ['随笔']
---

正文从这里开始……
```

## 为什么不用在线编辑器

- 文章即文件，**版本天然受 Git 管理**
- 本地编辑器（Vim / VS Code）比任何网页编辑器都快
- 迁移成本低：文件拷走就是全部家当

> 工具的意义在于让人忘记工具的存在。

## 草稿管理

frontmatter 里加一行 `draft: true`，构建时自动跳过，本地预览仍然可见。

## 发布

```bash
npm run deploy   # 构建 + 上传，一条命令
```

写完到上线，间隔不超过一分钟。
