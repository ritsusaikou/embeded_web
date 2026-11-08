// 站点全局配置：改这里就能改全站文案与链接
export const site = {
  name: '嵌入式系统课程项目站',
  brandMark: 'iM',
  brandName: 'EMBEDDED',
  brandSub: 'i.MX6ULL · 课程项目',
  tagline: '一个人、一块 512MB 的开发板、一个跑在板子上的网站',
  description:
    '嵌入式系统课程项目：i.MX6ULL 开发板上的静态站点托管实践。包含 8 个课程实验日志、Qt 课程设计、frp 内网穿透部署、系统设计文档与技术选型分析。',
  github: 'https://github.com/ritsusaikou/embeded_web',
  domain: 'imx6ull.example.com',
  board: 'i.MX6ULL · Cortex-A7 单核 792MHz',
  course: '嵌入式系统（2026 秋）',
  author: 'ritsusaikou',
  year: 2026
};

// 主要导航
export const nav = [
  { href: '/', label: '首页' },
  { href: '/lab/', label: '实验日志' },
  { href: '/project/', label: '课程项目' },
  { href: '/course-design/', label: '课程设计' },
  { href: '/docs/', label: '设计文档' },
  { href: '/notes/', label: '技术选型' },
  { href: '/about/', label: '关于' }
];

// 首页右下角"运行状态"快照（构建时值；上线后会被 /status.json 覆盖）
export const snapshot = {
  host: 'imx6ull-alpha',
  kernel: '4.1.15',
  uptime: '6d 04:12',
  mem: '58 / 512 MB',
  nginx: '1.14.2 · 1 worker',
  tunnel: 'frp 0.58 · 已连通',
  tls: 'TLS 1.3 · 公网终结',
  collectedAt: '2026-11-08 15:40'
};

// 首页指标条
export const metrics = [
  { num: '8', unit: '个', label: '课程实验全部完成并归档' },
  { num: '512', unit: 'MB', label: '目标运行环境内存上限' },
  { num: '3.2', unit: 'MB', label: '板上 Nginx 常驻内存' },
  { num: '0', unit: 'KB', label: '页面框架 JS（仅 1 段 0.4KB 状态脚本）' }
];
