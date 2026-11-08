---
title: 内网穿透方案对比与 frp 落地
order: 2
date: 2026-11-05
summary: 开发板在校园网 NAT 后面没有公网 IP，如何把 http://板子:80 映射到自己的域名。对比 frp / Cloudflare Tunnel / Tailscale，并说明 TLS 必须终结在公网服务器的原因。
tags: [frp, 内网穿透, 网络, TLS]
reading: 8 分钟
---

## 问题

开发板接在实验室交换机后面，出口是校园网 NAT，**没有公网 IP，也无法做端口映射**。但项目要求「用域名访问」。所以需要一条从「公网服务器」反向进入「板子」的通道。

关键认知：**穿透的方向是「从内向外建立连接」**。板子主动连服务器，把连接保持住，服务器收到请求时顺着这条已有连接转发回去。这样 NAT 设备不会拦——因为出站连接本来就是允许的。

## 方案对比

| 方案 | 板上客户端内存 | 需要公网服务器 | 优点 | 缺点 |
| --- | --- | --- | --- | --- |
| **frp** | 8–15 MB | 需要 | 纯 TCP 转发，可见可控，配置透明，答辩好讲 | 要自己维护 frps 与证书 |
| Cloudflare Tunnel | 15–25 MB | 不需要 | 一条命令搞定，自带 CDN 与证书 | 依赖第三方，流量过境，国内延迟不稳 |
| Tailscale / ZeroTier | 20–40 MB | 不需要 | 组网简单，P2P 直连 | 需装虚拟网卡，TUN 在嵌入式内核上要额外配置 |
| 花生壳等商业内网穿透 | — | 不需要 | 零配置 | 免费版限速限流量，域名不可控 |
| WireGuard 自建 | 3–8 MB | 需要 | 最省内存，性能最好 | 要在板子上配 TUN/TAP，路由与 iptables 较繁 |

选 **frp**：常驻内存小、逻辑最透明、每一层都能自己控制，而且它的「客户端主动外连」模型正好适合讲清楚 NAT 穿透原理。WireGuard 内存更省，但对这个项目来说配置成本高于收益。

## 架构

```text
浏览器 ──HTTPS:443──> 公网服务器 Nginx
                          │ proxy_pass 127.0.0.1:8080
                       frps（公网）
                          ▲ frp 隧道（板子主动外连 7000）
                       frpc（开发板）
                          │
                       Nginx:80 ──> dist/ 静态文件
```

## 两个必须做对的细节

### 1. frps 的回连端口只绑本机

frps 有两个端口概念：`bindPort`（板子连进来的 7000）和 `remotePort`（映射出来的 8080）。**后者绝对不能对公网开放**，否则任何人都能绕过你的 Nginx 直连站点，甚至把服务当跳板。

```toml
# /etc/frp/frps.toml
bindPort = 7000
proxyBindAddr = "127.0.0.1"   # 关键
auth.method = "token"
auth.token = "换成一串随机字符串"
```

```toml
# /etc/frp/frpc.toml
serverAddr = "服务器公网 IP"
serverPort = 7000
auth.method = "token"
auth.token = "与服务器一致"

[[proxies]]
name = "board-web"
type = "tcp"
localIP = "127.0.0.1"
localPort = 80
remotePort = 8080
```

### 2. TLS 终结在公网服务器

为什么不在板子上跑 certbot：

- Let's Encrypt 的 ACME 校验需要在 80 端口对外可见，而板子在 NAT 后面；
- ARM 上 openssl 握手与续期开销不小，还会周期性拉起 Python；
- 证书私钥放在一块随时可能被拔电的开发板上，本身就不合理。

正确做法：**证书只在公网服务器上，板子只说 HTTP，明文只在 frp 隧道里跑，而隧道本身是加密的（可用 `transport.tls.enable = true`）。**

```nginx
server {
    listen 443 ssl http2;
    server_name imx6ull.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## 板子侧的额外优化

单核 A7 做 gzip 压缩要几毫秒到十几毫秒，一次请求就白烧 CPU。改成**构建时预压缩**，Nginx 直接发 `.gz`：

```bash
find dist -type f \( -name '*.html' -o -name '*.css' -o -name '*.xml' \) -exec gzip -9 -k {} \;
```

```nginx
gzip_static on;   # 有 .gz 就发 .gz，没有就发原文件
gzip off;         # 关掉运行时压缩，省 CPU
```

## 踩过的坑

**板子重启后站点 502。** frpc 起来了但 Nginx 还没起，frps 转发过来无人应答。用 systemd 的 `After=nginx.service` 加 `Restart=always` 解决。

**校园网夜间断连。** frpc 默认有心跳，但网络设备会清理空闲连接的 NAT 表项。加 `transport.heartbeatInterval = 10`、`transport.heartbeatTimeout = 30` 后自动重连恢复正常。

**访问很慢，首字节 300ms+。** 起初以为是板子慢，实际是公网服务器到板子这条链路绕了远路，加上 TCP 窗口小。开启 `transport.tcpMux = true` 复用连接后改善明显。

## 结论

frp 的价值不只是「能用」，而是**每一层都可解释**：DNS 指向服务器、Nginx 持有证书、frps 只绑本机、frpc 主动外连、板子只提供静态文件。答辩时可以把这条链路从上到下讲一遍，每一跳的作用和风险都说得清——这比「我用了个工具就通了」有价值得多。
