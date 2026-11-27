# 部署笔记 · 开发板静态站点 + frp 反向隧道

> 照着一步一步做就能上线。每一步都写了「干什么 / 为什么 / 怎么做 / 怎么算成功」。
> 卡住的时候直接跳到附录 C 的坑表。

## 先说清楚：这份笔记用的是哪个方案

**主流程 = 方案 A：板子上跑 nginx 发文件。**

第 11 步配板上的 nginx、第 12 步让 frpc 转发给本机 80，**全篇 16 步都是围绕 A 写的**
（第 17 步是后加的板端 agent，可选，不影响这 16 步），
线上 `https://board.nexuslink.icu` 现在跑的就是 A。

**方案 B 在附录 A**（让 frpc 自己发文件，板上不用 nginx），只在板子装不了 nginx 时才考虑，
**当前没有启用**，留着是为了对照两种做法的差别、以及需要时快速切换。

### 为什么选 A 而不是 B

| 理由 | 具体说明 |
| --- | --- |
| 更充分地利用板子 | 题目的重点是"把网站部署到开发板上"。A 让板子的 nginx 真正承担静态文件服务；B 只是把目录交给 frpc，板子几乎不参与服务过程 |
| 有真实可调优的点 | `sendfile` 零拷贝、`gzip_static` 预压缩、`access_log off` 减少 SD 卡写放大、`worker_connections` 按 512MB 内存调小——每一条都能写进报告、答辩时讲得出所以然 |
| 可观测、可排错 | nginx 有 `error.log`、状态可查；`static_file` 插件出问题基本没有反馈 |
| 功能完整 | gzip 压缩、`Cache-Control` 缓存头、站点自己的 404 页，B 三者都没有 |
| 代价可接受 | 多约 5MB 内存、多一个常驻进程；实测整机只用 62/494MB，完全扛得住 |

一句话：**A 是"在板子上正经跑一个 Web 服务"，B 是"绕过去"**。

---

## 0. 先看全局

### 0.1 最后的链路长什么样


```
浏览器
  │  https://board.nexuslink.icu:443
  ▼
┌─────────────────────────────────────────┐
│ 阿里云服务器 112.124.58.134              │
│                                         │
│  nginx :443    ← 证书在这里，HTTPS 在这里解开
│    │ proxy_pass 127.0.0.1:8080
│    ▼
│  frps :7000  ← 等板子来连
│    │  （板子主动连过来的那条长连接）
└────┼────────────────────────────────────┘
     │  公网
     ▼
┌─────────────────────────────────────────┐
│ i.MX6ULL 开发板（家庭/宿舍内网）           │
│                                         │
│  frpc ──► 127.0.0.1:80                  │
│                │                        │
│             nginx :80  ← 发 /var/www/site 里的文件
└─────────────────────────────────────────┘
```

**关键理解**：板子没有公网 IP，外部连不进来。所以反过来——板子主动连服务器，
连上之后服务器把收到的请求顺着这条已经建好的连接往回送。这就是"反向隧道"。

### 0.2 名词对照（这些词第一次看很懵）

| 名词 | 是什么 | 在本项目里 |
| --- | --- | --- |
| frps | frp 的**服务端**，跑在公网服务器 | 监听 7000，等板子来连 |
| frpc | frp 的**客户端**，跑在开发板 | 主动连 7000，把 8080 的流量转给自己 |
| 反向代理 / 反代 | nginx 收到请求后转给另一个程序 | 443 收到请求 → 转给 127.0.0.1:8080 |
| TLS 终结 | 在某一层把 HTTPS 解开，后面走明文 | 在服务器 nginx 上解开，隧道内是明文 |
| ACME | 自动签发证书的协议 | Let's Encrypt 用它验证域名归你所有 |
| webroot | 一种验证方式：往网站目录放个文件让对面来读 | 文件放 `/var/www/letsencrypt` |

### 0.2.1 端口对照（最容易绕晕的地方）

| 端口 | 在哪一侧 | 谁在监听 | 谁去连它 | 作用 |
| --- | --- | --- | --- | --- |
| 443 | 服务器 | nginx，`0.0.0.0` | 浏览器 | 解 TLS，反代到 8080 |
| 8080 | 服务器 | frps，**只绑 `127.0.0.1`** | 同机的 nginx | 隧道出口，映射板的 80 |
| 7000 | 服务器 | frps，`0.0.0.0` | **板子的 frpc（主动外连）** | 隧道控制连接，长活 |
| 80 | 开发板 | 板上 nginx | 板上的 frpc | 真正读文件、发响应 |
| 7681 | 开发板 | board-agent，**只绑 `127.0.0.1`** | 板上 nginx（`/agent/` 前缀） | 状态 API + WebSocket shell/屏幕（第 17 步） |

**记住三句话**：

1. **7000 是"板子连进来"的入口**，所以必须在服务器上对公网开放（安全组放行）。
2. **8080 是"服务器内部交接"的端口**，只绑 `127.0.0.1`，外面碰不到。
3. **板子上既没有 7000 也没有 8080 的监听**。板子对外只是一个"客户端"，
   连出去时源端口是内核随机分配的高位端口（比如 54321 → 服务器:7000）。

还有一个时序细节：**8080 不是常驻监听**。要先有板子连上、注册了 proxy，frps 才会在
服务器上开出 8080。所以装完 frps 时 `ss` 里看不到 8080 是正常的，不是装错了。


### 0.3 本次部署的实际参数（换成你的就改这几处）

| 项 | 值 |
| --- | --- |
| 域名 | `board.nexuslink.icu` |
| 服务器公网 IP | `112.124.58.134` |
| 服务器登录 | `root` + 密钥 `_ECS-ubuntu.pem` |
| 板子 IP（静态，见 13.4） | `192.168.137.91` |
| 板子 MAC（固定，见 13.3） | `eth0 88:60:44:c2:46:16` |
| 板上站点目录 | `/var/www/site` |
| 站点源码目录 | `D:\Documents\大三上\嵌入式系统\项目\embeded_web` |
| frp token | `3edc37ef6bca4f276c4d0c6ba3c9421d`（两边必须一模一样） |
| 隧道端口 | 服务器 7000，映射出来 8080 |

### 0.4 开工前必须成立的三个前提

1. **域名 A 记录已指向服务器**。加一条 `board` → `112.124.58.134` 的 A 记录。
   验证：`nslookup board.nexuslink.icu 223.5.5.5` 要回 112.124.58.134。
   不是这个结果，后面签证书 100% 失败，先等解析生效。
2. **服务器安全组放行 80、443、7000**。阿里云控制台 → 安全组 → 入方向。
   服务器上 `ufw` 关了也没用，安全组是云厂商在外层拦的。
3. **板子能上网**（能访问 112.124.58.134）。板上 `ping -c 2 112.124.58.134` 验证。

---

## 1. 本机：把站点域名配对，构建出 dist

**干什么**：让 Astro 知道自己的公网地址，并生成纯静态文件。

**为什么**：`site` 这个字段决定了页面里的 canonical、RSS、sitemap 里写的绝对地址。
不改成真实域名，搜索引擎和微信分享拿到的都是 `example.com`。

**怎么做**

改 `astro.config.mjs`：

```js
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  site: 'https://board.nexuslink.icu',   // ← 改这里，用于 canonical / RSS / sitemap
  output: 'static',                       // 纯静态，产物是 html/css，不需要 node 运行
  trailingSlash: 'always',                // 每个页面输出成 目录/index.html
  compressHTML: true,
  integrations: [sitemap()],
  build: { inlineStylesheets: 'auto' }
});
```

构建（Windows PowerShell 里执行）：

```powershell
cd "D:\Documents\大三上\嵌入式系统\项目\embeded_web"
npx astro build
```

**怎么算成功**：控制台最后一行 `[build] Complete!`，并且 `dist\` 下有 23 个 html。

```powershell
(Get-ChildItem dist -Recurse -Filter *.html).Count    # 应该输出 23
```

> **绝不要在板子上执行 `npm install` / `npm run build`**。
> Astro 构建峰值内存超过 500MB，板子只有 494MB，必然 OOM；
> 严重时会把 sshd 一起挤掉，板子直接失联，只能重新插串口。

---

## 2. 服务器：装 nginx 和 certbot，先看清现状

**干什么**：装 Web 服务器和证书工具，但装之前先确认这台机器上已经有什么。

**为什么**：一台机器只能有一个进程监听 80/443。如果上面已经跑着别的站，
直接装会抢端口，把别人的站点打下来。先看清再动手。

**怎么做**

```bash
ssh -i 你的密钥.pem root@112.124.58.134
```

```bash
# ① 谁占着 80/443/7000
ss -lntp | grep -E ':(80|443|7000)\b'

# ② 已经启用了哪些站点（这个目录里的文件会被 nginx 加载）
ls -l /etc/nginx/sites-enabled/

# ③ 证书在不在
ls -l /etc/letsencrypt/live/ 2>&1

# ④ nginx 活着吗
systemctl is-active nginx

# ⑤ 装
apt update && apt install -y nginx certbot
nginx -v
```

**怎么算成功**：`nginx -v` 输出 `nginx/1.24.0`；`ss` 那一行显示 nginx 在听 80。

> 路径 `/usr/sbin` 有时不在 ssh 的 PATH 里，`nginx: command not found` 就写全路径
> `/usr/sbin/nginx -v`。

**常见坑：装完起不来**

如果 `systemctl status nginx` 是 failed，看日志：

```bash
journalctl -u nginx -n 20 --no-pager
```

如果看到 `cannot load certificate "/etc/letsencrypt/live/xxx/fullchain.pem"`，
说明这台机器**以前有过一份配置，引用了不存在的证书**，nginx 校验失败直接不启动。
这正是我们当时遇到的情况——整台机器的 80/443 全是空的。

处理：那份旧配置先挪走（不要删，改天可能要恢复），等证书签好再挪回来。见第 4 步。

---

## 3. 服务器：建 ACME 校验目录

> [!info] ACME 是什么
> **ACME** = **A**utomated **C**ertificate **M**anagement **E**nvironment，中文「自动化证书管理环境」。
> 它是 Let's Encrypt 使用的协议（RFC 8555），规定了「怎么申请、怎么证明域名归你、怎么取证书」
> 这一整套流程，certbot 就是它的客户端。

**干什么**：建一个空目录 `/var/www/letsencrypt`。

**为什么**：Let's Encrypt 要确认"这个域名真的指向你的服务器"，验证方法是：
它从外网访问 `http://你的域名/.well-known/acme-challenge/一串随机字符`，
只要读到的内容跟它给你的对得上，就认为域名归你。

那它读的物理文件从哪来？就是一会儿用 certbot 往这个目录里写。
所以目录要先存在，并且 nginx 要能把它暴露出去。

**怎么做**

```bash
mkdir -p /var/www/letsencrypt
ls -ld /var/www/letsencrypt
```

> [!note] `ls -ld` 里的 `-l` 和 `-d`
> - **`l` = long**：长格式输出（权限、所有者、大小、修改时间，就是 `ls -l` 的那个 l）
> - **`d` = directory**：把**目录本身**当成一个条目列出来，而不是列出里面的内容
>
> 光写 `ls -l /var/www/letsencrypt` 会列出目录里有什么（刚建好是空的，什么都看不到）；
> 加 `-d` 才看得到目录自己那一行的权限和属主。

**怎么算成功**：`drwxr-xr-x ... /var/www/letsencrypt`。

---

## 4. 服务器：先写 80 端口的临时配置（这一步是为了解开死锁）

**干什么**：让 nginx 暂时只监听 80，任何访问都返回 `acme-ok`。

**为什么**（整份笔记最重要的一处原理）：

最终的 nginx 配置里，443 那段要写：

```nginx
ssl_certificate /etc/letsencrypt/live/board.nexuslink.icu/fullchain.pem;
```

但**证书还没签，这个文件不存在**。nginx 启动时会校验这个文件，找不到就直接拒绝启动，
报 `cannot load certificate ... No such file or directory`。

于是就成了死锁：

- 要签证书 → 需要 nginx 在 80 上响应验证请求
- 要启动 nginx → 需要证书文件存在

**打破死锁的办法**：先加载一份"只有 80 段、不碰 443"的临时配置，
让 nginx 先跑起来，签完证书，再换成正式配置。

**怎么做**

```bash
# ① 停用已有的自定义站点（挪到备份目录，不是删除）
rm -f /etc/nginx/sites-enabled/default
mkdir -p /etc/nginx/sites-enabled-bak
mv /etc/nginx/sites-enabled/board     /etc/nginx/sites-enabled-bak/ 2>/dev/null
mv /etc/nginx/sites-enabled/nexuslink /etc/nginx/sites-enabled-bak/ 2>/dev/null
```
> [!note] `2>/dev/null` 在干什么
> - `2`：文件描述符，代表 **stderr（standard error，标准错误流）**，也就是程序的报错信息
> - `>`：重定向，覆盖写入
> - `/dev/null`：Linux 的黑洞设备，丢进去的数据直接被抛弃，不保存、不输出
>
> 合起来：**把报错丢掉，只留正常输出在屏幕上**。上面那几条 `mv` 后面加它，
> 是因为这些站点本来可能就不存在，报 `No such file` 属于预期，不想让它刷屏。

```bash
# ② 写临时配置
cat > /etc/nginx/sites-available/acme-temp <<'CONF'
server {
	listen 80;
    server_name board.nexuslink.icu;

    # ACME 验证请求走这里，文件从 /var/www/letsencrypt 里读
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type "text/plain";
    }

    # 其它请求先返回固定字符串，方便确认域名确实打到这台机器
    location / { return 200 "acme-ok\n"; }
}
CONF
```

> [!note] `<<'CONF'` 和 `CONF` 是 shell 的 here-doc，不是 nginx 语法
> `cat > 文件 <<'CONF'` 开始 here-doc：后面所有行**原样**喂给 `cat` 写进文件，
> 直到遇见单独一行的 `CONF` 为止。**这两行标记不属于 nginx 配置，别粘贴进 vim。**
>
> - `'CONF'` 的**单引号不能省**：加了引号才是「不做变量展开」，配置里的 `$host` 才会原样写下去；
>   不加引号，shell 会先把 `$host` 替换成空字符串，配置直接废掉
> - 结束标记必须**独占一行、顶格**写，且和开头完全一致（多一个空格都不认）
> - 标记名随便取（`CONF` / `EOF` / `NGINX` 都行），只要前后一致

```bash
# ③ 启用并重载
ln -sfn /etc/nginx/sites-available/acme-temp /etc/nginx/sites-enabled/acme-temp
nginx -t
systemctl restart nginx
```

> **为什么必须有这条 `ln -s`**：`sites-available` **只是一个仓库**，nginx 根本不读它。
> 真正被加载的只有 `sites-enabled/` 里的文件——`nginx.conf` 里写的是
> `include /etc/nginx/sites-enabled/*;`。配置写好了不建软链，等于没写。

> **还有个容易忽略的点**：`nginx -T` 打印的是**磁盘上的配置**，不是当前进程正在用的那份。
> 改完不 reload，`nginx -T` 看到的是新的、实际跑的仍是旧的。
> 判断"现在到底在跑什么"要看 `curl` 的结果，不是看配置文件。

**怎么算成功**

```bash
curl -s -H 'Host: board.nexuslink.icu' http://127.0.0.1/
```

返回 `acme-ok` 就对了。

**如果返回的是 301 而不是 `acme-ok`**

    301 Moved Permanently
    ---
    nginx/1.24.0 (Ubuntu)

这个 301 不是你写的 `acme-temp` 返回的——`acme-temp` 里没有任何 `return 301`。
它来自**别的站点**（那个站点里有 `location / { return 301 https://$host$request_uri; }`），
说明请求根本没匹配到你的临时 server 块。

按顺序查三条：

| # | 检查 | 命令 | 正常应该看到 |
| --- | --- | --- | --- |
| ① | 软链建了没 | `ls -l /etc/nginx/sites-enabled/` | `acme-temp -> /etc/nginx/sites-available/acme-temp` |
| ② | 配置里有没有这个 server_name | `nginx -T \| grep -n "server_name board"` | 能看到那一行 |
| ③ | 重载了没 | `systemctl reload nginx` | 再 curl 一次 |

**最常见的是第 ① 条**：只写了 `sites-available/acme-temp`，忘了 `ln -s` 到 `sites-enabled`。
补上再 reload 就好：

```bash
ln -sfn /etc/nginx/sites-available/acme-temp /etc/nginx/sites-enabled/acme-temp
nginx -t && systemctl reload nginx
curl -s -H 'Host: board.nexuslink.icu' http://127.0.0.1/    # 这次应该回 acme-ok
```

> [!note] `ln -sfn` 的四段分别是什么
> - `ln`：创建链接（link）
> - `-s`：**symbolic**，符号链接（软链接），不是硬链接
> - `-f`：**force**，目标已存在就直接覆盖
> - `-n`：**treat symlink to directory as normal file** —— 目标本身是「指向目录的软链接」时，
>   不要钻进去在目录里建新链接。`sites-enabled` 这种场景的标配，防踩坑

> [!note] `ls` 和 `ll` 不是一回事
> - `ls` = **list**，原生程序 `/bin/ls`
> - `ll` **没有英文全称**，它只是 shell 给 `ls -l` 起的外号（alias）。
>   换台没配别名的机器（比如这块板子的裸 shell）敲 `ll`，就是 `command not found`

> [!note] `ls -s` 的 `s`
> **`s` = size**：显示每个文件占用的**块大小**（磁盘占用），不是文件内容的字节数。
> 和 `ls -l` 的第五列对比着看，能发现「一个 100B 的小文件也占 4KB」这种块对齐现象。

> [!tip] 怎么反推 301 是谁返回的
> 看 `curl -I` 的 `Location` 头：`Location: https://board.nexuslink.icu/` 说明是
> `return 301 https://$host$request_uri;` 这条通用跳转干的，不是某个具体站点。

> `nginx -t` 不通过**千万别 restart**。restart 会把正在跑的旧进程也停掉，
> 站点直接全挂。先 `nginx -t` 通过再重载。

---

## 5. 服务器：申请证书

**干什么**：用 certbot 向 Let's Encrypt 要一张 90 天有效期的证书。

**为什么**：浏览器只认受信任 CA 签的证书。自己签的（自签名）浏览器会红字警告。

**怎么做**

```bash
certbot certonly --webroot -w /var/www/letsencrypt \
  -d board.nexuslink.icu \
  --agree-tos --no-eff-email -n
```

参数逐项解释：

| 参数 | 作用 |
| --- | --- |
| `certonly` | 只申请证书，**不改** nginx 配置 |
| `--webroot -w 目录` | 验证方式：往这个目录写文件，让对面来读 |
| `-d 域名` | 要签的域名，多个域名写多个 `-d` |
| `--agree-tos` | 同意服务条款 |
| `-n` | 非交互，不提问 |
| 行尾 `\` | 换行续写，实际可以打成一整行 |

> 为什么不用 `certbot --nginx`：那个模式会自动改写你的 nginx 配置文件。
> 改了什么看不见，出问题很难查。`certonly` 只往目录里丢一个文件，配置始终在自己手里。

**怎么算成功**

```
Successfully received certificate.
Certificate is saved at: /etc/letsencrypt/live/board.nexuslink.icu/fullchain.pem
This certificate expires on 2027-02-06.
```

> [!info] `.pem` 是什么
> **PEM** = **P**rivacy-**E**nhanced **M**ail（RFC 1421）。名字来自早年的加密邮件标准，
> **现在早就不用于邮件了**，只是这套「Base64 + 头尾标记」的封装格式被沿用下来，
> 用来装证书和公私钥 —— nginx / certbot 用的就是它。
>
> - 文本格式：Base64 编码，带 `-----BEGIN xxx-----` / `-----END xxx-----` 头尾标记
> - 后缀 `.pem`、`.crt`、`.key` 都**可能**是 PEM 格式，看内容比看后缀可靠
>
> 所以 `fullchain.pem` 不是什么特殊证书，就是「PEM 格式的证书链」；
> `privkey.pem` 就是同一格式的私钥。

自己复核一遍：

```bash
ls -l /etc/letsencrypt/live/board.nexuslink.icu/
openssl x509 -in /etc/letsencrypt/live/board.nexuslink.icu/fullchain.pem \
  -noout -subject -dates -issuer
```

看到 `subject=CN = board.nexuslink.icu`、`issuer=... Let's Encrypt`、有效期 90 天就对了。

**失败了怎么看**

| 报错 | 原因 |
| --- | --- |
| `DNS problem: NXDOMAIN` | A 记录没加，或还没生效（回到 0.4） |
| `Timeout during connect` | 80 端口外部不通，安全组没放行 |
| `Invalid response ... 404` | 临时配置里的 `root` 和 `-w` 指向的目录不一致 |

---

## 6. 服务器：换成正式的 443 配置，反代到 8080

**干什么**：80 只做跳转和续期验证，443 加载证书并把请求转给本机 8080。

**为什么**：
- 443 要有证书，浏览器才不报警；
- 转给 8080 是因为 frps 会在服务器上监听 8080（板子连上来之后）。
  nginx 不需要知道板子在哪，只需要知道"8080 有东西在听"。

**怎么做**

```bash
cat > /etc/nginx/sites-available/board <<'CONF'
# ---------- 80：只做 ACME 验证和跳转 ----------
server {
    listen 80;
    server_name board.nexuslink.icu;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type "text/plain";
    }

    location / { return 301 https://$host$request_uri; }
}

# ---------- 443：证书 + 反代到隧道 ----------
server {
    listen 443 ssl http2;
    server_name board.nexuslink.icu;

    ssl_certificate     /etc/letsencrypt/live/board.nexuslink.icu/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/board.nexuslink.icu/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;
    server_tokens       off;        # 响应头不暴露 nginx 版本号

    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;

    # 关键：与 frpc 的 remotePort 一致
    location / { proxy_pass http://127.0.0.1:8080; }
}
CONF
```

```bash
# 切到正式配置，把之前停用的站点恢复回来
rm -f /etc/nginx/sites-enabled/acme-temp
ln -sfn /etc/nginx/sites-available/board /etc/nginx/sites-enabled/board
mv /etc/nginx/sites-enabled-bak/* /etc/nginx/sites-enabled/ 2>/dev/null

nginx -t && systemctl reload nginx
```
> [!important] `ln -sfn` 的方向：源在前，链接在后
> `ln` = **link**，三个短选项可以合并成 `-sfn`：
>
> - `-s`：**symbolic**，符号链接（软链接），不是硬链接
> - `-f`：**force**，目标已存在就直接覆盖
> - `-n`：**treat symlink to directory as normal file** —— 目标本身是「指向目录的软链接」时，
>   不要钻进去在目录里建链接。`sites-enabled` 这种场景的标配，防踩坑
>
> 语法就一句：
>
> ```
> ln -sfn 源文件 目标链接
> ```
>
> - 前面 `/etc/nginx/sites-available/acme-temp` —— ✅ **必须预先存在**（你的配置本体）
> - 后面 `/etc/nginx/sites-enabled/acme-temp` —— 要**新建**的软链接；已存在时由 `-f` 覆盖
>
> **顺序记法：源在前（已存在），链接在后（新建）。** 写反了不报错，但会得到一个
> 指向不存在文件的死链，nginx 加载时才发现。

**怎么算成功**

```bash
curl -I http://board.nexuslink.icu                  # 301 跳到 https
curl -I https://board.nexuslink.icu                 # 502 —— 这是对的！
```

> [!note] `curl -I` 的 `-I` 是大写 i
> - **curl** = **Client URL**，向 URL 发请求的命令行工具
> - **`-I`**：大写 i，**HEAD** 请求（等价 `--head`）
>
> 作用：**只发 HEAD、只拿响应头、不下载响应体**，所以很快，适合看状态码、跳转和服务信息。
> 不加 `-I` 就是 GET，会把整个页面拉下来。

示例输出里第一行就是你最关心的：`HTTP/1.1 301 Moved Permanently`

**502 是正常状态**：TLS 已经通了、nginx 也在工作，只是 8080 那头还没人听
（板子还没连上来）。

> 想不依赖 DNS 在本机测：
> ``curl -sk -o /dev/null -w "%{http_code}\n" https://board.nexuslink.icu/ --resolve board.nexuslink.icu:443:127.0.0.1``
> URL 里的主机名要和 `--resolve` 里一致，写成 IP 会得到 `000`（连接失败）。

---

## 7. 服务器：装 frps

**干什么**：装 frp 服务端，让它监听 7000 等板子来连。

**为什么**：板子在内网，外部连不进去，只能让板子连出来。
frps 就是那个"等在公网上的接收端"。

**怎么做**

```bash
cd /tmp
curl -fsSL -o frp.tar.gz \
  https://ghfast.top/https://github.com/fatedier/frp/releases/download/v0.58.1/frp_0.58.1_linux_amd64.tar.gz
tar xzf frp.tar.gz
install -m 755 frp_0.58.1_linux_amd64/frps /usr/local/bin/frps
frps -v
```

> [!note] `curl -fsSL -o` 拆开看
> 短选项可以合并，`-fsSL` 就是 `-f -s -S -L`：
>
> - `-f` = **fail**：HTTP 返回 4xx/5xx 时直接返回非 0，**不把错误页存成文件**（脚本靠它判断成败）
> - `-s` = **silent**：关掉进度条和无关输出
> - `-S` = **show error**：配合 `-s`，静默但**仍然打印错误信息**
> - `-L` = **location**：跟随 301/302 跳转（GitHub、镜像站几乎都跳，必须加）
> - `-o` = **output**：**小写 o**，指定保存到本地的文件名
>
> 一句话：**静默下载、自动跟跳转、出错报错、失败不留坏文件、存成指定名字。**
>
> 顺便区分大小写：`-O`（大写）是**用远程文件名**保存，`-o`（小写）是**你自己指定**文件名。

> [!note] `install -m 755` 比 `cp` 更适合部署程序
> `install` 是自带命令，**复制 + 设权限**一步做完：
>
> - `-m` = **mode**，就是 `chmod` 那套数字权限
> - `755`：属主 `rwx`、同组 `r-x`、其他人 `r-x`
>
> 等价于：
>
> ```
> cp frps /usr/local/bin/frps
> chmod 755 /usr/local/bin/frps
> ```
>
> 后面那句 `frps -v` 是装完立刻跑一次，`-v` = version，验证部署成功。

> [!note] `tar xzf` 拆开
> `-x` = extract 解压、`-z` = gzip、`-f` = file 指定包名（**f 必须放最后、紧跟包名**）。
> 三个短选项可以合并写，`tar xzf` 和 `tar -xzf` 等价。

> `https://ghfast.top/` 是 GitHub 加速前缀。境内直连经常几十秒只下到几百 KB。
> 包名里的 `amd64` 对应 x86_64 服务器，ARM 服务器换 `arm64`，先 `uname -m` 确认。

写服务端配置：

```bash
mkdir -p /etc/frp
cat > /etc/frp/frps.toml <<'CONF'
# 板子来连的端口
bindPort = 7000

# 【关键】映射出来的 8080 只绑本机，外部连不到
proxyBindAddr = "127.0.0.1"

# 口令，必须和板子上的 frpc.toml 完全一致
auth.method = "token"
auth.token = "3edc37ef6bca4f276c4d0c6ba3c9421d"

# 只允许板子注册这个端口段，防止被注册到意外端口
allowPorts = [{ start = 8080, end = 8089 }]
CONF
```

> **`proxyBindAddr` 为什么必须是 `127.0.0.1`**：frps 打通隧道后，会在服务器上
> 真的监听 8080。如果写成 `0.0.0.0`，外面的人就能直接访问
> `http://112.124.58.134:8080`，绕过 nginx、绕过 HTTPS 拿到站点内容，证书就白做了。
> 绑 `127.0.0.1` 后只有同机的 nginx 能转过去。

写 systemd 服务（常驻、开机自启、崩了自动重启）：

```bash
cat > /etc/systemd/system/frps.service <<'UNIT'
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
UNIT

systemctl daemon-reload
systemctl enable --now frps
```

> `daemon-reload` 不能省：新增了 service 文件，systemd 必须重读才知道有这个服务。

**怎么算成功**

```bash
systemctl is-active frps            # active
ss -lntp | grep 7000                # *:7000 后面跟着 frps
journalctl -u frps -n 5 --no-pager  # frps started successfully
```

此时 **8080 还没监听，这是正常的**——板子还没连上来。

顺便确认 7000 从公网能通（本机 PowerShell）：

```powershell
$c = New-Object System.Net.Sockets.TcpClient
$c.ConnectAsync("112.124.58.134",7000).Wait(4000); $c.Connected    # 期望 True
$c.Close()
```

False 就去安全组加 7000/TCP。

---

## 8. 板子：先连上去，摸清环境

**干什么**：搞清楚架构、系统类型、有没有 nginx、IP 是多少。

**为什么**：板子是 arm，服务器是 x86，软件包不通用；还要知道它有没有 nginx、
有没有 systemd，这决定后面怎么配。

**怎么连**

**① 串口**（网络没配好时唯一的入口）
Xshell 新建会话 → 协议 SERIAL → COM9、115200、8 数据位、1 停止位、校验 None、
**流控 None**。回车出现 `root@ATK-IMX6U:~#` 就连上了。

> 流控选错了能连上但收不到任何输出，这个坑很容易卡住。

**② SSH**（网络通了之后更快，推荐）

```bash
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.137.91
```

> 两个 `-o` 参数不能省。板上是 2016 年的 dropbear，只支持老算法 `ssh-rsa`，
> 现代客户端默认拒绝，不加会报 `no matching host key type found`。scp 同样要带。

> [!tip] 不想每条命令都敲两个 `-o`：写进 `~/.ssh/config`
> 本机是 OpenSSH 10.x，`ssh-rsa`（SHA-1）在 8.8 之后默认禁用，所以每一条 `ssh` / `scp`
> 都得补上 `-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa`，
> 漏一条就报 `no matching host key type found. Their offer: ssh-rsa`。
>
> 一劳永逸的办法是给板子建个别号。Windows 下文件在 `C:\Users\<你>\.ssh\config`，
> Linux/macOS 在 `~/.ssh/config`：
>
> ```
> Host board
>     HostName 192.168.137.91
>     User root
>     HostKeyAlgorithms +ssh-rsa
>     PubkeyAcceptedAlgorithms +ssh-rsa
> ```
>
> 之后所有命令都能简写成：
>
> ```bash
> ssh board
> scp deploy/frpc.init board:/etc/init.d/frpc
> ```
>
> 注意 `Host` 后面那个词（`board`）是你自己起的名，和板上跑的服务无关，别和 `frpc` 搞混。
> 想确认别名生效：`ssh -G board | grep -i ssh-rsa`。
>
> 还有一点：本文所有命令都是 bash 写法，换行用行尾 `\`。**在 PowerShell 里行尾 `\` 不续行**
> （它用反引号 `` ` ``），要么写成一行，要么加反引号：
>
> ```powershell
> scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa deploy/frpc.init root@192.168.137.91:/etc/init.d/frpc
> ```

**采集环境**

```bash
uname -a                        # 要看到 armv7l
cat /etc/issue                  # Freescale i.MX Release Distro
ps -p 1 -o comm=                # init → sysvinit，没有 systemd
free -m | head -2               # 内存 494MB
df -h / | tail -1               # 根分区剩余
ip -4 addr show | grep 'inet '   # 记下 eth0 的 IP
/usr/sbin/nginx -v              # 有没有 nginx，什么版本
date                            # 时钟对不对
```

> [!info] `/bin`、`/sbin`、`/usr` 分别放什么（FHS 标准）
> **FHS** = Filesystem Hierarchy Standard，文件系统层次标准。
>
> **`/bin` = Binary**：**基础、必要的用户命令**（`ls`、`cat`、`cp`、`mv`、`rm`、`bash`、`pwd`、`echo`），
> 普通用户也能执行。它属于根文件系统，**系统最早启动阶段（还没挂 `/usr`）就必须可用**。
> 现代发行版多用 **usr-merge**，`/bin` 只是个指向 `/usr/bin` 的软链接。
>
> **`/sbin` = System Binary**：**系统管理、运维**命令（`ifconfig`、`ip`、`mount`、`umount`、`fsck`、
> `reboot`、`shutdown`、`fdisk`），大多要 root。**普通用户和非登录 shell 的 PATH 通常不含它** ——
> 这就是板上敲 `nginx` 会 `command not found`、必须写 `/usr/sbin/nginx` 的原因。
> usr-merge 后 `/sbin -> /usr/sbin`。
>
> **`/usr` = Unix System Resources**（**不是 user**，这里最容易误会）：系统绝大部分软件、库、
> 头文件、文档都在这，是最庞大的目录。历史上它确实叫 user，后来 FHS 重新定义为
> Unix System Resources；用户家目录在 `/home`，不在 `/usr`。

> [!note]- `/usr` 下面的结构（和根目录很像）
> ```
> /usr
> ├── bin     # 大部分普通用户程序（现代主流 bin 都在这里）
> ├── sbin    # 大部分系统管理程序
> ├── lib     # 32 位共享库 / 系统库
> ├── lib64   # 64 位共享库
> ├── include # C/C++ 头文件，编译用
> ├── share   # 共享资源：man 手册、文档、图标、配置模板、locale 语言包
> ├── local   # 自己编译安装的软件放这里，不和包管理器冲突
> │   ├── bin
> │   ├── sbin
> │   ├── lib
> │   └── share
> ├── src     # 内核源码（可选）
> └── games   # 小游戏（很少用）
> ```
> `/usr/local` 是重点：**自己装的软件放这儿**，跟包管理器互不干扰 ——
> 本站的 frpc、board-agent 都装在 `/usr/local/bin`。

**实测结果**（用来对照你的板子）

| 项     | 值                           | 影响                            |
| ----- | --------------------------- | ----------------------------- |
| 架构    | `armv7l`                    | 下载 frpc 要用 `linux_arm` 包      |
| PID 1 | `init`                      | 没有 systemd，自启要写 `/etc/init.d` |
| nginx | `1.8.1`（已装，`S92nginx` 开机自启） | 可以直接用它发文件                     |
| 内存    | 494MB 总，用 62MB              | 不能在板上构建                       |
| 时钟    | 可能停在几个月前                    | RTC 掉电不保时，不影响隧道，但日志时间会乱       |

**注意**：`nginx` 直接敲会 `command not found`，因为 ssh 的非登录 shell PATH 里没有
`/usr/sbin`。要用绝对路径 `/usr/sbin/nginx`。

---

## 9. 板子：装 frpc

**干什么**：把 arm 版的 frp 客户端放到板子上。

**为什么**：13MB 用串口传要十几分钟，让板子从服务器 `wget` 下来只要几秒。
所以先在服务器上准备一份，放在 80 端口能下载到的位置。

**怎么做**

**① 服务器：下载 arm 版并放进下载目录**

```bash
cd /tmp
curl -fsSL -o frp_arm.tar.gz \
  https://ghfast.top/https://github.com/fatedier/frp/releases/download/v0.58.1/frp_0.58.1_linux_arm.tar.gz
tar xzf frp_arm.tar.gz
file frp_0.58.1_linux_arm/frpc
```

`file` 要输出 `ELF 32-bit LSB executable, ARM, EABI5 ... statically linked`。
`statically linked` 很重要——静态链接不依赖板子的 libc 版本，拷过去就能跑。

```bash
mkdir -p /var/www/letsencrypt/.well-known/acme-challenge/dl
install -m 644 frp_0.58.1_linux_arm/frpc \
  /var/www/letsencrypt/.well-known/acme-challenge/dl/frpc
```

> 为什么放这个奇怪路径：80 端口上已经有 `/.well-known/acme-challenge/` 这个
> location 能直接下文件，不用为传文件单独开端口。

**② 板子：拉下来**

```bash
mkdir -p /etc/frp /usr/local/bin /var/www/site
cd /usr/local/bin
wget -O frpc http://112.124.58.134/.well-known/acme-challenge/dl/frpc
chmod +x frpc
./frpc -v
```
> [!note] `wget -O frpc` 和 `chmod +x frpc`
> - `wget`：Linux 的文件下载工具；`-O frpc` 是**大写 O**，指定**下载后存成什么文件名**
>   （不用服务器上原本的名字）。整条意思：**从这个 HTTP 地址下载，存到当前目录、文件名 `frpc`**
> - `chmod`：改权限；`+x` 是加**可执行位（execute）**。刚下载的文件默认没有执行权限，
>   不 `chmod +x` 直接跑会 `Permission denied`
> - `./frpc -v` 前面的 `./` 不能省：当前目录不在 PATH 里，写 `frpc` 会找不到命令

**怎么算成功**：最后一行输出 `0.58.1`。

**③ 传完就删掉服务器上那份**

```bash
rm -rf /var/www/letsencrypt/.well-known/acme-challenge/dl
```

那是公网任何人都能下载的 13MB 文件，留着不合适。

---

## 10. 板子：放站点文件

**干什么**：把本机 `dist/` 的内容放到板子的 `/var/www/site`。

**为什么**：板上 nginx 的 `root` 指向这个目录，文件不在就 404。

**怎么做**

**① 本机打包**（方案 A 必须连带 `.gz`，见第 11.3 步）

> [!warning] 顺序：**先压 `.gz`，再打 tar**
> `gzip_static on` 不是「帮你压」，而是「旁边有个同名 `.gz` 就直接发那个」。
> 所以 `.gz` 必须在 `tar` **之前**生成，否则进不了包，板上白压一遍。
> 完整说明在 11.3，这里就是连着做这两步：

```bash
cd dist
find . -type f \( -name '*.html' -o -name '*.css' -o -name '*.js' -o -name '*.xml' \
  -o -name '*.json' -o -name '*.svg' \) -exec gzip -9 -k -f {} \;
cd ..
```

`-k` 保留原文件（不然只剩 `.gz`，不支持 gzip 的客户端拿不到内容），`-f` 覆盖已有 `.gz`。

Git Bash 里：

```bash
cd /d/Documents/大三上/嵌入式系统/项目/embeded_web
tar czf site.tar.gz -C dist .
```

参数：`c` 创建、`z` gzip 压缩、`f` 输出文件名、`-C dist .` 先切到 dist 再打包
（这样解出来没有多余的 `dist/` 一层）。

**没有 Git Bash**：用 Xftp 把整个 `dist` 文件夹拖到服务器 `/tmp/dist`，
然后在服务器上打包：

```bash
tar czf /var/www/letsencrypt/.well-known/acme-challenge/dl/site.tar.gz -C /tmp/dist .
```

**② 传到板子**

```bash
scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    site.tar.gz root@192.168.137.91:/tmp/
```
> [!note] `scp` 是什么，和 `wget` 差在哪
> **scp** = **S**ecure **C**opy **P**rotocol，走 SSH 通道传文件，**需要 SSH 账号/密钥**，
> 和 `wget`（走 HTTP、拉公开文件、不用登录）完全是两条路。
>
> ```
> scp 本地文件 user@远程IP:远程路径      # 本地 → 远程
> scp user@远程IP:远程文件 本地路径      # 远程 → 本地
> scp -r 本地目录 user@ip:/xxx          # 目录要加 -r（recursive）
> ```
>
> ```
> scp frpc.toml root@192.168.1.100:/etc/frp/    # 传一个配置文件
> scp -r root@192.168.1.100:/var/log ./board-log # 把日志目录拉回来
> ```
>
> 板上这个 2016 年的 dropbear 只认 `ssh-rsa`，所以 `scp` 也得带
> `-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa`；
> 用 Xftp 直连同样要放开这条算法。

**③ 板上替换**

```bash
ls /var/www/site                       # 先确认路径对不对，再执行下一条！
rm -rf /var/www/site/*
tar xzf /tmp/site.tar.gz -C /var/www/site
```

> [!info] `/var` 为什么叫 variable
> **`/var` = variable**：存放**运行过程中会持续变化、动态生成**的数据。
> 对比一下分工：`/usr` 是装好基本不变的静态程序，`/etc` 是配置，`/var` 是会长的那些。
>
> - `/var/log` 日志、`/var/run` 运行时 pid 与 socket、`/var/tmp` 临时文件（重启不一定清空）
> - `/var/www` —— **网页站点根目录的传统约定**，Nginx/Apache 默认往这放。
>   不是系统强制，放 `/home/www` 也能跑，只是大家都习惯 `/var/www`
>
> 板上 `/var/log` 是软链到 tmpfs 的，断电即丢，所以本站日志特意写到 `/var/lib/` 下面（见 13.1）。

> [!note] `tar czf` / `tar xzf` 的参数与 `-C`
> - `c` = **c**reate 打包、`x` = e**x**tract 解包
> - `z` = gzip 压缩（后缀 `.tar.gz`）
> - `f` = **f**ile 指定包名，**必须放最后、紧跟包名**
> - `-C DIR` = `--directory`，**先切到该目录再打包/解包**
>
> 两种用法：
>
> ```
> tar czf 包.tar.gz -C 源目录 .     # 打包：只把 源目录 里的内容打进包，不带多余的目录层
> tar xzf 包.tar.gz -C 目标目录      # 解包：解到 目标目录
> ```

**怎么算成功**

```bash
find /var/www/site -type f | wc -l     # 方案 A 应该是 58（29 个原文 + 29 个 .gz）
ls /var/www/site | head
```

> `rm -rf /var/www/site/*` 里那个 `/*` 很关键：它是"目录里的东西"，
> 不带 `*` 就变成删目录本身了。执行前一定先 `ls` 看一眼。

---

## 11. 板子：配 nginx（方案 A · 主流程）

### 11.1 为什么板子的 nginx 需要先改主配置

**原理**：Debian/Ubuntu 的 nginx 默认在 `nginx.conf` 里写
`include /etc/nginx/conf.d/*.conf;`，所以往 `conf.d/` 扔一个文件就生效。
但 Yocto 出厂的 `nginx.conf` **没有这一行**，而且 `conf.d/` 目录压根不存在。

不补的话，站点配置放进去 nginx 根本不读，现象是"80 端口还是返回 nginx 默认页"，
很容易误以为配置写错了。

**怎么做**

```bash
mkdir -p /etc/nginx/conf.d
cp -a /etc/nginx/nginx.conf /etc/nginx/nginx.conf.orig     # 备份原配置

cat > /etc/nginx/nginx.conf <<'CONF'
user  www-data;
worker_processes  1;              # 单核 A7，开 1 个就够
error_log  /var/log/nginx/error.log warn;
pid        /run/nginx/nginx.pid;

events {
    worker_connections  512;      # 512MB 内存跑不满 1024，调小更稳
}

http {
    include       mime.types;
    default_type  application/octet-stream;

    sendfile      on;             # 零拷贝，静态文件必备
    tcp_nopush    on;
    keepalive_timeout 15;

    include /etc/nginx/conf.d/*.conf;    # ← 关键：Yocto 默认没有这一行
}
CONF
```

### 11.2 写站点配置

```bash
cat > /etc/nginx/conf.d/site.conf <<'CONF'
server {
    # 板上 nginx 1.8.1 编译时没带 --with-ipv6，
    # 写 listen [::]:80 会报 "INET6 sockets are not supported"，所以只监听 IPv4
    listen 80 default_server;

    server_name _;                 # 匹配任意域名，隧道过来的 Host 是域名

    root  /var/www/site;           # 站点文件目录
    index index.html;

    charset utf-8;
    server_tokens off;             # 响应头不暴露 nginx 版本

    sendfile   on;
    tcp_nopush on;

    # 压缩：关掉运行时压缩，改发预压缩好的 .gz
    gzip off;
    gzip_static on;

    access_log off;                # 关访问日志，减少 SD 卡写入（卡写坏就用不了）

    client_body_timeout    5s;
    client_header_timeout  5s;
    keepalive_timeout      15s;
    send_timeout           10s;

    # 带内容哈希的静态资源可以长期缓存（文件名变了 URL 就变，不怕缓存旧内容）
    location /_astro/ {
        add_header Cache-Control "public, immutable, max-age=31536000";
    }

    # 状态快照不缓存
    location = /status.json {
        add_header Cache-Control "no-store";
        try_files $uri =404;
    }

    # HTML 不缓存，改完立刻生效
    location / {
        try_files $uri $uri/ $uri.html =404;
        expires -1;
    }

    # 用站点自己的 404 页面
    error_page 404 /404.html;
    location = /404.html {
        internal;
        expires -1;
    }

    location ~ /\.(?!well-known) {
        deny all;
    }
}
CONF
```

几个配置逐个说：

| 配置 | 作用 / 原因 |
| --- | --- |
| `listen 80 default_server;` | 只有一个站点，让它当默认站。**不要写 `[::]:80`** |
| `server_name _;` | 下划线表示"任意 Host"，隧道过来的域名不固定也能匹配 |
| `root /var/www/site;` | 文件在哪，改这里就能换根目录 |
| `gzip off; gzip_static on;` | 不发实时压缩，改发同名 `.gz`。A7 压一个页面要几毫秒，纯属浪费 CPU |
| `try_files $uri $uri/ $uri.html =404;` | 依次找：原路径 → 当目录找 index.html → 补 `.html` → 都没有才 404 |
| `expires -1;` | 让浏览器每次都问服务器（HTML 常变，不能缓存） |
| `error_page 404 /404.html;` | 走站点自己的 404 页，而不是 nginx 默认页 |
| `access_log off;` | 减少 SD 卡写放大，需要统计时临时打开 |

> `/_astro/` 那里**不要同时写 `expires 1y;` 和 `add_header Cache-Control`**，
> 那样会输出两个 `Cache-Control` 头。只留 `add_header` 一条。

### 11.3 预压缩 .gz（`gzip_static` 的前提）

**原理**：`gzip_static on` 不是"帮你压"，而是"旁边有个同名 `.gz` 就直接发那个文件"。
所以文件必须提前压好。

**为什么在 PC 上压**：PC 快得多。板上也能压，但要占 CPU 和 SD 卡写入。

```bash
cd dist
find . -type f \( -name '*.html' -o -name '*.css' -o -name '*.js' -o -name '*.xml' \
  -o -name '*.json' -o -name '*.svg' \) -exec gzip -9 -k -f {} \;
cd ..
```

`-k` 保留原文件（不然只剩 `.gz`，不支持 gzip 的客户端拿不到内容），`-f` 覆盖已有 `.gz`。

这一步要在**打包之前**做，`.gz` 才会一起进 tar 包。

### 11.4 检查并重载

```bash
/usr/sbin/nginx -t
/usr/sbin/nginx -s reload
```

**怎么算成功**

```bash
# ① 首页能打开
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/

# ② 压缩生效（关键）
curl -s -H "Accept-Encoding: gzip" -D - -o /dev/null http://127.0.0.1/ | grep -i content-encoding
# 期望：Content-Encoding: gzip

# ③ 体积对比
curl -s -o /dev/null -w "未压缩 %{size_download}B\n" http://127.0.0.1/
curl -s -H "Accept-Encoding: gzip" -o /dev/null -w "压缩后 %{size_download}B\n" http://127.0.0.1/
# 实测：27192B → 6739B

# ④ 404 走站点自己的页面
curl -s http://127.0.0.1/nope | grep -a -o "<title>[^<]*</title>"
# 期望：<title>页面不存在 · 嵌入式系统课程项目站</title>
```

---

## 12. 板子：配 frpc 并启动（方案 A 的最后一环）

**干什么**：告诉 frpc 连哪台服务器，以及把服务器 8080 的流量转给本机 80。

**为什么**：这是整条链路的倒数第二环。板子主动连 7000，之后 8080 收到的每个请求
都会被转给 `127.0.0.1:80`（板上的 nginx）。

> 这里的 `localIP` / `localPort = 80` 是**方案 A 的特征**：frpc 只做搬运，
> 真正发文件的是板上 nginx。方案 B 会把这两行换成 `[proxies.plugin] static_file`，
> 见附录 A。两种写法只能选一种，同时写会冲突。

**怎么做**

```bash
cat > /etc/frp/frpc.toml <<'CONF'
serverAddr = "112.124.58.134"     # 服务器公网 IP
serverPort = 7000                 # 与 frps 的 bindPort 一致

auth.method = "token"
auth.token = "3edc37ef6bca4f276c4d0c6ba3c9421d"    # 必须与 frps.toml 一模一样

# 【重要】开机时网络往往还没就绪。默认 true 会让 frpc 登录失败一次就退出，
# 表现为每次重启站点都 502、手动启动又立刻好。设 false 让它一直重试。
loginFailExit = false

transport.tls.enable = true       # 公网传输再加密一层
transport.heartbeatInterval = 10  # 心跳，防止路由器清掉空闲 NAT 表项
transport.heartbeatTimeout = 30
transport.tcpMux = true

[[proxies]]
name = "board-web"
type = "tcp"
localIP = "127.0.0.1"             # 板上的 nginx
localPort = 80
remotePort = 8080                 # 映射到服务器 8080，与 nginx 的 proxy_pass 对应
CONF
```

先前台跑一遍，看清日志：

```bash
date -s "2026-11-21 17:00:00"     # 校时（换成当下时间），只影响日志时间戳
/usr/local/bin/frpc -c /etc/frp/frpc.toml
```

> [!note] `-c` 不能省：`frpc /etc/frp/frpc.toml` 这种写法是错的
> frp v0.58 里配置文件是**标志**（flag），不是位置参数。源码 `cmd/frpc/sub/root.go` 原文：
>
> ```go
> rootCmd.PersistentFlags().StringVarP(&cfgFile, "config", "c", "./frpc.ini", "config file of frpc")
> ```
>
> 而 `RunE` 里只用了 `cfgFile` 这个变量，**根本没读 `args`**。所以：
>
> - `frpc -c /etc/frp/frpc.toml` ✅ 正确
> - `frpc /etc/frp/frpc.toml` ❌ 那个路径会被**静默忽略**，frpc 转头去当前目录找默认的
>   `./frpc.ini`：找不到就报文件不存在；当前目录恰好有配置时更坑 —— 它会拿别人的配置跑起来
>
> 怎么确认配置真的喂进去了：正常启动的第一行日志就是
>
> ```
> start frpc service for config file [/etc/frp/frpc.toml]
> ```
>
> **方括号里必须是你给的路径**。如果看到的是 `./frpc.ini` 或这行压根没有，就是参数没生效。

**怎么算成功**：看到这两行

```
login to server success, get run id [xxxxxxxx]
[board-web] start proxy success
```

然后 `Ctrl+C` 结束前台进程。**同时到服务器上看一眼**：

```bash
ss -lntp | grep 8080          # 应该出现 127.0.0.1:8080
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/    # 200
```

到这一步，外网访问 `https://board.nexuslink.icu` 就已经能打开站点了，
剩下的只是让它开机自动跑。

---

## 13. 板子：开机自启与断电自愈

**先说结论**：这块板子是 **Yocto + sysvinit**，没有 systemd。`systemctl`、`systemctl enable`、`daemon-reload` 全部不存在，别照抄网上的 systemd 教程。

> ⚠️ 别和第 7 节搞混：第 7 节给**公网服务器**装 frps 时用的是 `systemctl enable --now frps`，
> 那是 Ubuntu（PID 1 是 systemd）。板子这边是另一套。两者的区别和四个常见误解见 **13.7**。

- 自启 = `/etc/init.d/<服务>` + `/etc/rc5.d/S<NN><服务>` 软链，数字越小越早执行
- **sysvinit 没有 `Restart=always`**：进程退出不会自动重启，必须自己补一个巡检
- `/var/log` 是指向 `/var/volatile/log`（tmpfs）的软链，**断电即丢**，日志要写到 rootfs 上

`deploy/` 下三个文件对应三件事：

| 文件 | 装到板上 | 作用 |
| --- | --- | --- |
| `deploy/frpc.init` | `/etc/init.d/frpc` | frpc 启停、开机自启、日志持久化 |
| `deploy/keepalive.sh` | `/usr/local/bin/board-keepalive.sh` | cron 每分钟巡检 nginx/frpc，兼开机校时 |
| `deploy/set-mac.init` | `/etc/init.d/set-mac` | connman 启动前固定 MAC，见 13.3 |
| `deploy/install-board.sh` | 放板上任意位置执行 | 上面三个脚本内嵌其中，一键安装，幂等 |

**一键装**（自包含，只传这一个文件）：

```bash
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.137.91 'sh /tmp/install-board.sh'
#   加 --dry-run 只打印计划不落盘
#   加 --no-ip   跳过静态 IP
```

它会自检环境、固定 MAC、配静态 IP、装自启、加 cron、配校时，最后打一张汇总表。重复执行不会重复加 cron 或覆盖正确配置。脚本开头有 `MAC_ETH0 / MAC_ETH1 / STATIC_IP / GATEWAY` 几个变量，换板子只改那里。想手动一步步来，继续往下看。

### 13.0 原理：开机时到底发生了什么

板子加电、内核启动完之后，做的最后一件事是执行 `/sbin/init`（这块板是 `init.sysvinit`）。
后面所有服务都由它按 `/etc/inittab` 的指令拉起，而 inittab 里真正相关的只有三行：

```
id:5:initdefault:                       ① 默认进入 runlevel 5
si::sysinit:/etc/init.d/rcS             ② 系统初始化阶段 → 跑 rcS.d/
l5:5:wait:/etc/init.d/rc 5              ③ 进入 runlevel 5 → 跑 rc5.d/
```

> [!tip] 为什么是 `5`：runlevel 的编号，不是缩写
> `id:5:initdefault:` 和 `l5:5:wait:/etc/init.d/rc 5` 里的 `5`，是 **runlevel（运行级别）的编号**，
> 就是「第 5 档」。它没有任何单词首字母的含义，纯数字，但每个数字是**约定俗成**的：
>
> - `0` 关机、`6` 重启、`1` 单用户（只给 root 修系统）、`S` 启动初始化（就是上面的 rcS）
> - `2~5` 多用户。Debian / Yocto 把这四档做得几乎一样，真正的差别在「这一档挂了哪些软链」
>
> 这块板的分工很直白，可以直接对比出来：`rc5.d` 挂了 `S01xserver-nodm`（拉 GUI），
> `rc3.d` 里对应的是 `K01xserver-nodm`（进 3 档就把 GUI 停掉）。
> 所以把 inittab 改成 `id:3:initdefault:` 也能开机，只是会停在命令行。

两条链路展开就是：

```
si  →  /etc/init.d/rcS  →  exec /etc/init.d/rc S  →  /etc/rcS.d/S*     地基：mount / udev / devpts / urandom
l5  →  /etc/init.d/rc 5                            →  /etc/rc5.d/K* S*  服务：connman / dropbear / nginx / crond
```

注意 `rcS` 自己几乎不干活，它只设 `runlevel=S` 然后 `exec /etc/init.d/rc S`；真正干活的是 `rc`。
关机时是 `rc 0`、重启时是 `rc 6`，所以 rc0.d / rc6.d 里既有 `K`（停服务）也有 `S`（收尾：卸载文件系统）。

#### 术语表

| 名字                | 是什么                                                                                   | 在这块板上                                                                     |
| ----------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| **runlevel**      | 系统的「状态编号」，init 靠它决定该跑哪套脚本                                                             | `0`=关机 `1`=单用户 `2~5`=多用户 `6`=重启 `S`=启动初始化。这块板写死 `id:5:initdefault:`，永远进 5 |
| **rc**            | `/etc/init.d/rc`，runlevel 的**执行器**。用法 `rc <级别>`，扫 `/etc/rc<级别>.d/`，先跑 `K*` 再跑 `S*`    | 就是它逐个执行软链                                                                 |
| **rcS**           | `/etc/init.d/rcS`，启动阶段的固定入口，本质是 `exec /etc/init.d/rc S`（这里的 S 就是 runlevel S，不是 Start） | inittab 的 `sysinit` 行调它                                                   |
| **rcS.d**         | runlevel `S` 的脚本目录，只在启动最早期跑一次，管「地基」                                                   | 18 个：psplash、sysfs、mountall、udev、checkroot、devpts、urandom…                |
| **rc5.d**         | runlevel `5` 的脚本目录，服务层。**你要加的开机自启就挂这里**                                               | 25 个：networking、connman、dropbear、nginx、crond…                             |
| **rc0.d / rc6.d** | 关机 / 重启时的目录，各有 30 个链接                                                                 | `K*` 停服务，`S*` 做收尾                                                         |
| **S / K**         | `S`=Start（启动），`K`=Kill（停止）                                                            | 开机跑 S，关机跑 K                                                               |
| **S99 里的 99**     | 执行顺序号，两位零填充                                                                           | 按文件名排序 = 按数字排序，所以 `S04` 必须小于 `S05connman`                                 |

> [!tip] `rc` 是缩写，但不是「runlevel control」
> `rc` = **runcom** → 再展开是 **run commands**（「要跑的一串命令」）。
>
> 这个词来自 1960 年代 MIT 的 CTSS：那里把「一批按顺序执行的命令」存成文件，叫 *runcom*。
> UNIX 继承了这个习惯，用 `.rc` 后缀表示「启动时读的命令文件」—— 所以你早就见过它的亲戚：
> `~/.bashrc`、`~/.vimrc`、`/etc/inputrc`，全都是「开机/启动时照着跑的一串设定」。
>
> 放到 init 这套里，`rc` 就是 `/etc/init.d/rc` 那个 shell 脚本本身：
> **按 runlevel 编号，进 `/etc/rcN.d/` 把里面的软链跑一遍**。
> 顺带排除两个常见误猜：不是 `resource control`（那是 cgroup 的词），也不是 `runlevel config`。

> [!tip] 两个 `S` 不是同一个 `S`（最容易绕晕的地方）
> `rcS` 的 S 和 `S99frpc` 的 S 长得一样、含义完全不同，它们是两层的概念：
>
> - **`rcS` / `rcS.d` 里的 `S`** = **runlevel `S`**（S 取 single-user，单用户；在这类系统里它就是
>   「启动最早那一档」）。它是**级别名** —— 和 `rc5` 的 `5` 是同一层的东西，只不过一个是字母、一个是数字。
> - **`S99frpc` 里的 `S`** = **Start**（启动），与 `K01frpc` 的 `K` = **Kill**（停止）配对。
>   它是**动作前缀**，后面跟两位顺序号，再后面才是服务名。
>
> 一句话：`rcS.d` 的 S 回答「哪一档」，`S99` 的 S 回答「启动还是停止」。

#### sysvinit 到底指什么（为什么它和 systemd 不是一回事）

> [!note] 名字怎么拆：`sys` 是 system，`v` 是**罗马数字 5**
> `sysvinit` = **System V init**，念作 "System Five init"。
>
> - **`sys`** = system
> - **`v`** = 罗马数字 **Ⅴ**，即「第五版」—— **不是哪个单词的首字母**
> - **`init`** = initialization，就是 PID 1 那个进程
>
> 所以它没有「全称缩写」这回事：`sysvinit` 只是「System V 的 init」连写，指的是
> 1983 年 AT&T UNIX System V 引入的那套 init 设计（`/sbin/init.sysvinit` 就是这么来的）。
> 也有人顺口念成 "sys-v-init"，因为拼写上那个 v 单看确实像个字母 —— 但它代表的是版本号，
> 和 `rc5.d` 里的 `5` 是同一类东西：**数字/序号，不是缩写**。

上面这套机制叫 **SysV 风格 init**（System V，AT&T UNIX 第五版，1983 年的设计），
`sysvinit` 是它的具体实现。但严格说要分成两部分，很多人把它们混成一个东西：

| 组件                                                                   | 由谁提供                       | 板上证据                                         |
| -------------------------------------------------------------------- | -------------------------- | -------------------------------------------- |
| `init`（PID 1）、`halt`、`reboot`、`shutdown`、`runlevel`、`sulogin`、`last` | **sysvinit** 包，C 程序        | `/sbin/init -> /sbin/init.sysvinit`          |
| `rc`、`rcS`、`rc.local`、`functions`                                    | **initscripts** 包，shell 脚本 | `/etc/init.d/rc` 是 4.4KB 的脚本，`rcS` 只有 525 字节 |

关键点：**init 二进制只负责「读 `/etc/inittab`，按行把命令执行掉」**，它根本不知道 `rcN.d` 是什么东西。
「按 runlevel 遍历 `rcN.d` 目录、先跑 `K*` 再跑 `S*`」这一整套行为，是 `rc` 这个 **shell 脚本**实现的约定。

所以准确的说法是：**sysvinit 提供引擎，initscripts 提供排班规则。**

顺带一个能解释上一条 Tips 的事实：板上带 `.sysvinit` 后缀的 alternatives 目标有
`halt / init / mountpoint / pidof / poweroff / reboot / runlevel / shutdown`，
但**没有任何 `.systemd` 后缀的文件** —— 这就是 `systemctl` 在板上根本不存在的底层原因。
（`/usr/bin/update-alternatives` 是存在的，说明镜像在构建时只在 busybox 版和 sysvinit 版之间做选择。）


#### sysvinit 和 systemd 的本质差异

|        | sysvinit（这块板）        | systemd（公网服务器）                             |
| ------ | -------------------- | ------------------------------------------ |
| 顺序怎么定  | **数字大小**，纯串行         | unit 里 `After=` / `Requires=` 建**依赖图**，可并行 |
| 服务是什么  | shell 脚本             | 声明式 unit 文件（`.service`）                    |
| 启动一个服务 | 直接 `exec` 脚本         | 按依赖拉起前置服务、建 cgroup 隔离                      |
| 崩溃自动重启 | **没有**，必须自己补 cron 巡检 | `Restart=always`                           |
| 改完配置   | **不需要 reload**       | 必须 `daemon-reload`                         |
| 日志     | 服务自己往文件写             | journald 统一收                               |
| 加一个自启  | 放脚本 + 挂 `rc5.d` 软链   | 写 unit + `systemctl enable`                |

一句话：sysvinit 是「按号码排队挨个跑脚本」，systemd 是「先说清谁依赖谁，然后并行拉起来」。
前者简单到能背下来，后者功能强但概念多 —— 512MB 的板子上用 sysvinit 很合适。

> 所以第 7 节（服务器 Ubuntu 22.04）用 `systemctl`，本节（开发板 Yocto）用 rc 软链，
> 两台机器两套系统，命令不能互搬。

#### 三个够用的查看命令

```bash
/sbin/runlevel           # 当前级别，实测输出 "N 5"（N=上一次为空，即刚启动）
                         # 注意要带路径：非登录 shell 的 PATH 只有 /usr/bin:/bin，/sbin 不在里面
ls /etc/rc5.d/           # 这一级会跑什么；列表顺序就是执行顺序
ls -l /etc/rc5.d/S92nginx   # 看软链指向哪个脚本（-> ../init.d/nginx）
```

> 一句话记法：**`init.d` 是仓库，`rc*.d` 是排班表，`inittab` 决定上哪个班。**

---

### 13.1 frpc 开机自启

**为什么**：`rc5.d` 里原本只有 `S92nginx`，frpc 没有任何自启条目，板子一重启公网就断，得手动 `frpc -c` 才恢复。nginx 是 `S92`，frpc 用 `S99` 排在它后面。

> **注意：脚本放进 `/etc/init.d/` 不等于会自启。** 这个目录只是「脚本仓库」，真正决定开机跑什么的是
> `/etc/rcS.d/` 和 `/etc/rc5.d/` 里的软链（由 `/etc/inittab` 选 runlevel 5 决定）。板上实测：
> `init.d` 里 59 个脚本，`rcS.d` 只挂了 18 个，`rc5.d` 只挂了 25 个。所以 `cp` 完之后**必须**做下面
> 那个 `ln -sf`，否则脚本永远不会被调用。
>
> 同理：**`scp` 只是把文件搬过去，不是"安装"**。它既不建软链、也不启动服务，
> 传输成功只能说明"板上多了一个文件"。真正的安装是下面那条 `ssh` 里的三件事。

**怎么做**

同样两条命令，**两种终端写法不同**，照抄错版本会直接报 `usage: scp …`（见下面的 warning）。

**① bash（Git Bash / WSL / macOS / Linux）**

```bash
scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    deploy/frpc.init root@192.168.137.91:/etc/init.d/frpc    # 只放进仓库，还不算自启
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.137.91 '
  chmod 755 /etc/init.d/frpc
  ln -sf /etc/init.d/frpc /etc/rc5.d/S99frpc
  ln -sf /etc/init.d/frpc /etc/rc0.d/K01frpc
  ln -sf /etc/init.d/frpc /etc/rc6.d/K01frpc
  /etc/init.d/frpc start
'
```

**② PowerShell（Windows 默认终端）—— 一条一行，行尾不要加 `\`**

```powershell
scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa deploy/frpc.init root@192.168.137.91:/etc/init.d/frpc
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.137.91 "chmod 755 /etc/init.d/frpc; ln -sf /etc/init.d/frpc /etc/rc5.d/S99frpc; ln -sf /etc/init.d/frpc /etc/rc0.d/K01frpc; ln -sf /etc/init.d/frpc /etc/rc6.d/K01frpc; /etc/init.d/frpc start"
```

第一条只搬文件，第二条里做三件事：

1. `chmod 755` —— 补可执行位（git 里记的是 644，clone 下来没有 x）
2. 三条 `ln -sf` —— **这才是"开机自启"**：`S`=Start（拉起）、`K`=Kill（停止），
   `99` 是顺序号，排在 `S92nginx` 之后；`rc0.d` / `rc6.d` 的 K 负责关机、重启时先停
3. `start` —— 立刻启动一次，不用等重启

> [!warning] PowerShell 里照抄 bash 版会报 `usage: scp [-346ABCOpqRrsTv] …`
> 不是参数写错了，是 PowerShell 的解析规则和 bash 不一样，有**两个**坑：
>
> ```
> PS> scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
>     deploy/frpc.init root@192.168.137.91:/etc/init.d/frpc
> usage: scp [-346ABCOpqRrsTv] [-c cipher] [-D sftp_server_path] …
> ```
>
> （真机里敲完第一行会看到 `>>` 提示符，那是 PowerShell 以为你还没输完；
> 但 `scp` 收到的参数里已经混进了一个孤零零的 `\`，所以最后还是失败。）
>
> 1. **行尾 `\` 在 PowerShell 里不是续行符**（bash 才是），它会被当成**一个参数**原样传给 `scp`，
>    `scp` 于是认为"有个源文件叫 `\`"，参数校验失败就打印 usage
> 2. **单引号字符串不能跨行**。bash 版第二条 `ssh … '` 到下一行才结束，PowerShell 里这是语法错误；
>    跨行必须用 here-string（`@'` … `'@`）
>
> 解决办法二选一：用上面 ② 的"一条一行"版本；或者干脆切到 Git Bash 再粘 bash 版。
> 判断办法很直观：只要输出里出现 `usage:`，就是**参数没喂进去**，跟 key、密码、网络都无关。

> [!warning] 报 `no matching host key type found. Their offer: ssh-rsa` 怎么办
> 这是**客户端和板子的算法谈不拢**，与密码、权限、防火墙都无关：
>
> ```
> Unable to negotiate with 192.168.137.91 port 22: no matching host key type found.
> Their offer: ssh-rsa
> C:\WINDOWS\System32\OpenSSH\scp.exe: Connection closed
> ```
>
> - 板子上是 2016 年的 **dropbear**，只会 `ssh-rsa`（基于 SHA-1）
> - 本机 OpenSSH 从 **8.8 起默认禁用** `ssh-rsa`，所以连接阶段就被拒
>
> 给**每一条** `ssh` / `scp` 加两个 `-o`（`+` 表示「在默认列表追加」，不会挤掉现代算法）：
>
> ```powershell
> scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa deploy/frpc.init root@192.168.137.91:/etc/init.d/frpc
> ```
>
> **`scp` 和 `ssh` 是两个程序，各要带一次**：上面 `scp` 传完，紧接的那条 `ssh` 忘了加，
> 会以同一个错再挂一次。嫌麻烦就按 §8 的办法在 `~/.ssh/config` 里建个 `board` 别号，
> 之后 `ssh board`、`scp 文件 board:/路径` 都不用带参数。
>
> 而且这两个错**很容易混**：`no matching host key type` 是**密钥算法**被拒（连不上）；
> `usage: scp …` 是**参数**没解析对（根本没开始连）。看到 `usage` 就别再改 key 了，先看续行符。

> [!tip] 上传后为什么叫 `/etc/init.d/frpc`，不会和 `frpc` 那个程序重名吗
> **不会重名冲突 —— 名字一样，但不在同一条路上，谁被调用看「有没有写路径」。**
>
> 板上实测的两条 PATH：
>
> | 场景 | PATH | 裸敲 `frpc` 命中谁 |
> | --- | --- | --- |
> | 登录 shell（`. /etc/profile`） | `/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` | `/usr/local/bin/frpc`，**真程序** |
> | 非登录 ssh（`ssh root@… '命令'`） | `/usr/bin:/bin` | 找不到，`command not found` |
>
> - `frpc -v` 能出版本号，是因为**裸名字靠 PATH 找**，`/usr/local/bin/frpc` 排在最前面。
> - **`/etc/init.d` 永远不在 PATH 里**，所以裸敲 `frpc` 无论如何都碰不到脚本。
>   脚本只有两种被执行的方式：写全路径 `/etc/init.d/frpc start`，
>   或者被 `rc` 通过软链 `/etc/rc5.d/S99frpc start` 调起。
> - 脚本自己也不裸调 `frpc`：里面写死 `DAEMON=/usr/local/bin/frpc`。
>   开机那一刻 PATH 可能还没配全，只有绝对路径才稳。
> - 想确认某个 `frpc` 到底是谁：`type frpc` / `command -v frpc`。
>
> **那为什么还要叫一样的名字？** 因为 sysvinit 的约定是 `/etc/init.d/<服务名>`：
>
> - `rc` 从软链 `S99frpc` 剥出 `frpc` 当服务名，`K01frpc` 也得叫 `frpc` 才对得上（见下一条）
> - `service frpc start`、`update-rc.d frpc defaults` 都是按这个名字找文件
> - 而且两者互不相干：脚本里用绝对路径调二进制，二进制根本不知道脚本存在
>
> **仓库里为什么叫 `frpc.init`**：那只是给人看的后缀，一眼区分"这是 init 脚本，不是二进制"。
> 严格说 `rc` 只认**软链名**、执行的是软链，目标文件叫什么都能跑；但落盘叫 `frpc`
> 才能和 `service` / `update-rc.d` / 各种文档对得上，所以上传时就改名。
>
> 一个例外要记：**别让软链以 `.sh` 结尾**。`rc` 里有 `case "$1" in *.sh)` 分支，
> 对 `.sh` 是 `source` 执行而不是 `exec`，行为会变。

> [!tip] 软链名字为什么要 `S99frpc` 和 `K01frpc` 同名，数字却不必相同
> 因为 `rc` 是**靠「去掉前缀 + 两位数字之后剩下的名字」来配对**的。板上 `/etc/init.d/rc` 原文：
>
> ```sh
> for i in /etc/rc$runlevel.d/S*          # 逐个扫 S 开头的软链
> do
>     suffix=${i#/etc/rc$runlevel.d/S[0-9][0-9]}   # S99frpc → frpc
>     stop=/etc/rc$runlevel.d/K[0-9][0-9]$suffix   # → K??frpc
>     ...
>     startup $i start                     # 执行的是软链本身，带 start 参数
> done
> ```
>
> 由此推出四条硬规则：
>
> - `S99frpc` ↔ `K01frpc` **能配对**：两边剥掉 `S99` / `K01` 之后都叫 `frpc`，**序号不需要一致**
> - `S99frpc` ↔ `K01frpcd` 配不上：名字不同，关机时它找不到对应的 K 脚本，就不会被停
> - 名字必须是 `S`/`K` + **两位数字**：剥离用的是 `S[0-9][0-9]`，K 循环的 glob 是 `K[0-9][0-9]*`；
>   写成 `Sfrpc`、`S9frpc` 都剥不出服务名，也匹配不到，等于挂了个死链
> - `rc` 跑的是**软链本身**（不是它指向的文件名），所以软链必须指向一个可执行、
>   且能接受 `start` / `stop` 参数的脚本 —— 这也是为什么 `deploy/frpc.init` 里要写 `case "$1" in` 分支
>
> 另外 `0`/`6` 两个级别特殊：它的 S 脚本也会被当成 `startup $i stop` 调用，
> 所以关机时 `rc0.d` 里那些 `S*` 是「收尾」而不是「启动」。

> [!tip] 为什么一个脚本就能管开机、关机、重启
> 因为上面三条软链**指向同一个文件**，`rc` 调它时只换参数：
>
> ```
> rc5.d/S99frpc  →  /etc/init.d/frpc start     # 进 runlevel 5：拉起
> rc0.d/K01frpc  →  /etc/init.d/frpc stop      # 关机：优雅停
> rc6.d/K01frpc  →  /etc/init.d/frpc stop      # 重启前：先停
> ```
>
> 脚本里那句 `case "$1" in start) … ;; stop) … ;; esac` 就是在接这个参数。
> 所谓「开机自启 + 关机优雅停止」，本质是：**一份脚本、三条软链、一个参数**。
>
> 几个容易忽略的细节：
>
> - `rc` 只会传 `start` / `stop` 两个词，**不会**传 `restart`、`status`。
>   那两个是给人用的，写不写都不影响开机自启
> - **runlevel 0/6 时，连 `S*` 开头的脚本也会被当作 `stop` 调用**
>   （`rc` 里 `case "$runlevel" in 0|6) startup $i stop`），所以关机那趟别指望 `S*` 去「启动」什么
> - 正因为同一个文件会被反复调用，`start` 必须**幂等**：先 `is_running` 判断，
>   已经在跑就 `exit 0`，而不是再拉一个进程出来抢端口
> - 你手动敲 `/etc/init.d/frpc status` 走的是同一个 `case` 分支 ——
>   脚本并不关心「现在是开机还是我在手敲」，它只看 `$1`
> - 参数既不是 `start` 也不是 `stop` 时落到 `*)` 分支，打印用法并 `exit 1`，防止手滑

**脚本里两个和网上抄来的版本不一样的关键点**

1. 日志写 `/var/lib/frpc/frpc.log`，**不写 `/var/log`** —— 后者在 tmpfs 上，断电后一份证据都留不下
2. `start` 前先 `wait_net`（最多等 30s 探通 frps）再拉起，配合 `frpc.toml` 里的 `loginFailExit = false`，避免"开机时网络还没就绪 → 登录失败一次就退出 → 站点一直 502"

**怎么算成功**：`frpc running (pid xxx)`，并且日志里出现

```
login to server success, get run id [...]
[board-web] start proxy success
```

> [!warning] `status` 说 running，站点却还是 502 —— 进程在跑 ≠ 隧道通了
> `status` 只检查「pid 文件存在 + 这个进程还活着」（`is_running` 里就是 `kill -0`），
> 它**不验证有没有连上服务器**。所以这两件事要分开看：
>
> ```bash
> /etc/init.d/frpc status        # frpc running (pid 1031)   ← 只说明进程在
> tail -5 /var/lib/frpc/frpc.log # 这里才说明隧道通没通
> ```
>
> 日志里两类完全不同的结果：
>
> | 日志 | 含义 | 该去查 |
> | --- | --- | --- |
> | `login to server success` + `start proxy success` | 隧道通了 | 还不通就看服务器 nginx / 证书 |
> | `dial tcp 112.124.58.134:7000: i/o timeout` | **板子出不了网**，包到不了服务器 | 板子的网关/路由、PC 的网络共享 |
> | `login to server failed: authorization failed` | 能连上，但 token 不对 | 两边 `auth.token` 是否一致 |
> | `connect to server error: connection refused` | 连得到 IP，但 7000 没在听 | 服务器 frps 是否在跑、安全组 |
>
> 想快速分清「进程 / 出网 / 服务端」三层：
>
> ```bash
> ping -c 2 192.168.137.1     # ① 网关通不通（通只说明二层没问题，不代表能出网 —— 见 13.5）
> ping -c 2 112.124.58.134    # ② 服务器通不通（不通 = NAT/共享/路由的问题，跟 frpc 无关）
> ```
>
> 另外脚本里的 `wait_net` 开机时会最多等 30 秒探通服务器，**但等不到也会照常启动**
> （`loginFailExit = false`），所以"开机 30 秒内没起来"是预期，不是脚本写错。

### 13.2 进程守护（补上 sysvinit 没有的 Restart=always）

**为什么**：sysvinit 只负责"开机拉起来一次"。frpc 被 OOM 杀掉、nginx 崩掉，没有任何东西会管它——systemd 有 `Restart=always`，这里只能靠外部巡检。

巡检现在管**三样**：`nginx`、`frpc`、`board-agent`（第 17 步的），外加开机校时。

> [!info] `cron` 和 `crontab` 是什么
> - **`cron`**：一个常驻后台的进程，每分钟醒一次，看有没有到点的任务。名字来自希腊语 *chronos*（时间），**不是缩写**。
> - **`crontab`** = cron + table，**cron 的时间表**：一个文本文件，一行一条任务。
> - **`crontab` 命令**：管理这张表的工具。它不执行任务、不启动进程，只负责改表。
>
> 所以 `crontab` 有两重身份：是**文件名**（表落在 `/var/spool/cron/crontabs/root`），也是**命令名**（`/usr/bin/crontab`）。
>
> 命令只有四个动作：
>
> | 用法 | 全称 | 作用 |
> | --- | --- | --- |
> | `crontab -l` | list | 打印当前表（小写 `L`，不是数字 `1`） |
> | `crontab -e` | edit | 编辑表，存盘即生效 |
> | `crontab -r` | remove | 删掉**整张**表，无确认提示 |
> | `crontab -` | — | 从标准输入读一整份，**整份替换**现有表 |
>
> 为什么不直接 `vi` 那个文件：它和它所在目录有 `600` 和属主限制，改坏权限 cron 会拒收整张表；
> 而且手改没有校验，字段写错只是静默跳过那一行，你还以为任务在跑。

**怎么做**

```bash
scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    deploy/keepalive.sh root@192.168.137.91:/usr/local/bin/board-keepalive.sh
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.137.91 '
  chmod 755 /usr/local/bin/board-keepalive.sh
  (crontab -l 2>/dev/null; echo "* * * * * /usr/local/bin/board-keepalive.sh") | crontab -
  crontab -l
'
```

> [!note] 这三条各做什么
> **① `chmod 755`**：脚本 `scp` 过来是 `644`（没有 `x`），cron 到点执行会 `Permission denied`，
> 而且只往系统日志丢一行、不通知你。补成 `755` 才有执行权（`755`/`644` 怎么读见下一条）。
>
> **② `(crontab -l …; echo "…") | crontab -`**：cron **没有「追加一行」的选项**，只能整份覆盖写，
> 所以只能「先读出旧的 → 拼上新的一行 → 整份写回去」。圆括号是让两条命令的输出合成一个整体再送进管道。
>
> `* * * * *` 是五个时间字段：**分 时 日 月 周**，全 `*` = 每分钟一次（所以巡检延迟最长 60 秒）。
> 命令写 `/usr/local/bin/board-keepalive.sh` 全路径，是因为 **cron 的 `PATH` 只有 `/usr/bin:/bin`**——
> 跟你登录后的 PATH 不是一回事，写相对名字会 `command not found`。
>
> **③ `crontab -l`**：回读确认。该看到原有那条 `ntpdate-sync` 还在，后面多出你新加的一行。
>
> 两个副作用：`crontab -` 是整份替换，所以**别改写成** `echo "…" | crontab -`（会把原有任务全抹掉）；
> 这条命令也不去重，跑两次表里就有两行。真要重来，先 `crontab -e` 看一眼。

> [!important] `755`、`644` 里那几组 `rwx` 分别是谁的
> 一个数字拆成三组，对应**三类人**：
>
> | 位置 | 给谁 | `755` | `644` |
> | --- | --- | --- | --- |
> | 第 1 组 | 属主 owner（文件是谁的） | `rwx` = 7 | `rw-` = 6 |
> | 第 2 组 | 同组 group | `r-x` = 5 | `r--` = 4 |
> | 第 3 组 | 其他人 others | `r-x` = 5 | `r--` = 4 |
>
> 每组里的三个字母是三个开关相加：**`r` = 4（读）、`w` = 2（写）、`x` = 1（执行）**。
> 所以 `7 = 4+2+1 → rwx`，`6 = 4+2 → rw-`，`5 = 4+1 → r-x`，`4 = r--`。
>
> 同一个字母，对**文件**和**目录**意思不一样：
>
> | | `r` | `w` | `x` |
> | --- | --- | --- | --- |
> | 文件 | 能看内容 | 能改内容 | 能当程序执行 |
> | 目录 | 能列出里面有哪些文件 | 能在里面增删文件 | 能 `cd` 进去 |
>
> 目录的 `x` 最容易忽略：只有 `r` 没有 `x` 时，`ls` 能列出名字却打不开里面任何文件。
>
> 回到这两条命令：`755` = 属主可读写可执行、其他人只能读和执行，**脚本必须有 `x` 才能被 cron 跑起来**；
> `644` = 属主可读写、其他人只读，是普通文件/配置的默认值，`scp` 传上来的东西通常就是这个。
>
> 顺带：`ls -l` 最左边那 10 个字符，第一位是**类型**（`-` 普通文件、`d` 目录、`l` 软链），后面 9 位才是上面这三组。

### 13.3 固定 MAC —— 静态 IP 设了又失效的真正原因

**现象**：`connmanctl config ... --ipv4 manual` 明明设了静态 IP，重启后又变回 DHCP 分到的另一个地址。

**根因**：设备树里 `local-mac-address` 是全 0

```bash
od -An -tx1 /sys/firmware/devicetree/base/soc/aips-bus@02100000/ethernet@02188000/local-mac-address
#  00 00 00 00 00 00
```

内核 fec 驱动拿不到可用 MAC，就 `eth_hw_addr_random()` 随机生成一个（所以首字节常见 `32:`、`5e:`，那是"本地管理位"置位的标志）。而 **connman 的以太网服务是按 MAC 命名的**：

```
ethernet_886044c24616_cable     ← 配了静态 IP 的那个，MAC = 88:60:44:c2:46:16
ethernet_32b800d5eeed_cable     ← 某次开机随机出来的，DHCP 拿到了 192.168.137.171
```

MAC 一变服务名就变，上次配的 static 永远匹配不上，只能退回 DHCP。

**怎么做**：在 `connman`（`rc5.d/S05connman`）之前把 MAC 钉死。

```bash
scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    deploy/set-mac.init root@192.168.137.91:/etc/init.d/set-mac
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.137.91 '
  chmod 755 /etc/init.d/set-mac
  ln -sf ../init.d/set-mac /etc/rc5.d/S04set-mac    # S04 必须小于 S05connman
'
```

**怎么算成功**：重启后

```bash
cat /sys/class/net/eth0/address     # 88:60:44:c2:46:16，每次开机都一样
connmanctl services                 # ethernet_886044c24616_cable
```

顺手删掉随机 MAC 留下的废弃配置，免得 `connmanctl services` 看着乱：

```bash
ls /var/lib/connman/
rm -rf /var/lib/connman/ethernet_32b800d5eeed_cable    # 只删不是 886044c24616 的那些
```

> 更根治的办法是在 U-Boot 里 `setenv ethaddr 88:60:44:c2:46:16`、`setenv eth1addr aa:60:44:c2:46:16`、
> `saveenv`，设备树会被正确 fixup，连 `set-mac` 脚本都省了。但这块板子的镜像没带 `fw_setenv`，
> 只能从串口进 U-Boot 手敲，风险更高，所以选了 Linux 层兜底。

### 13.4 静态 IP

前提：**PC 侧这个网段固定是 `192.168.137.0/24`、网关固定 `192.168.137.1`**（用 ICS 还是 WinNAT 都一样，改不了）；PC 侧也不支持按 MAC 绑定租约。所以固定 IP 只能在板子侧做。

用 connman，**别动 `/etc/network/interfaces`**——那套配置本身是坏的（`auto eth0` 却没有 eth0 的 iface 段，eth1 写了两遍），而且 connman 会接管：

```bash
connmanctl services      # 确认服务名，形如 ethernet_886044c24616_cable
connmanctl config ethernet_886044c24616_cable --ipv4 manual 192.168.137.91 255.255.255.0 192.168.137.1
connmanctl config ethernet_886044c24616_cable --nameservers 223.5.5.5 119.29.29.29
```

参数顺序是 `--ipv4 manual <IP> <掩码> <网关>`，写错会静默失败。配置落在 `/var/lib/connman/<服务名>/settings`，rootfs 是 rw 的 ext3，重启和断电都保留。

**DNS 填公网的 `223.5.5.5`，别填 `192.168.137.1`。** ICS 理论上会代做 DNS 转发，但那个代理和 NAT 是两套组件，
经常一起没起来——那时 IP 通、域名不通（13.5 有完整解释）。

板上 `/etc/resolv.conf` 是 connman 的 dnsproxy，改它没用（会被覆盖），只能用
`connmanctl config <服务名> --nameservers ...`。

### 13.5 PC 侧联网：ICS 为什么每次重启都要手动开关一次

**现象**：开机后板子能 `ping 192.168.137.1`，但 `ping 112.124.58.134` 全丢包，frpc 报 `i/o timeout`；
到 Windows 里把「Internet 连接共享」关掉再打开一次，立刻恢复。板子上什么都没改。

**根因：ICS 的 NAT 只在「共享被开启的那一刻」建一次表，之后不再重建。**

`SharedAccess` 服务开启共享时会做两件事：给下游网卡设 `192.168.137.1` 并起 DHCP/DNS 代理；在 `ipnat.sys` 里建 NAT 表。
**这张表绑的是「当时那一对接口」，不是网段。** 开机时 USB 网卡（接板子的那个）枚举比内置网卡慢，
ICS 起来时下游还没就绪 → NAT 那半边没建成；稍后网卡就绪、地址补上了（所以能 ping 通），但转发路径还是空的。
关开一次共享 = 重新应用属性 = 重建表。

**「能 ping 通网关」会把你带偏**：`192.168.137.1` 是 PC 自己的地址，ICMP 由本机协议栈直接回，
**根本不经过 NAT**。它只证明二层和地址是对的，对「能不能出网」零信息量。

**同一个根因还有第二个症状：DNS 一起坏。** DNS 代理和 NAT 是两套独立组件，可能只建好了一个。
表现是 `ping 112.124.58.134` 通、`ping baidu.com` 一直卡着——连 `PING baidu.com (x.x.x.x)` 都不打印，
说明卡在解析阶段，一个包都没发出去。

#### 解法：换成 WinNAT

两者的根本差别在**匹配单位**：

| | 绑定什么 | 网卡重新枚举 / 插拔后 |
| --- | --- | --- |
| **ICS** | 一对具体接口（「把 A 共享给 B」） | 绑定失效，必须手动关开 |
| **WinNAT** | 一个网段（`192.168.137.0/24` 算内网） | 不受影响，出口每次按路由表现选 |

所以换过去之后，插拔网口、`ifIndex` 变化都不用管。

**切换顺序不能乱**，先做能回滚的那步：

**① 板子先改静态 IP**（此时 ICS 还通着，配错能连回来）

```bash
connmanctl services
connmanctl config ethernet_xxxxxx_cable --ipv4 manual 192.168.137.91 255.255.255.0 192.168.137.1
connmanctl config ethernet_xxxxxx_cable --nameservers 223.5.5.5
```

> [!warning] 这一步的前提是 13.3 已经做完
> connman 的服务名按 MAC 命名，这块板每次开机 MAC 都随机 → 服务名跟着变 → 静态 IP 对不上。
> 没固定 MAC 就配静态 IP，下次重启会失效，看起来像「静态 IP 又不管用了」。

**② PC 上关掉 ICS 共享**：网络连接 → 那块 USB 网卡 → 属性 → 取消「允许其他网络用户…」

**③ 建 NAT**（管理员 PowerShell）

```powershell
New-NetNat -Name BoardNat -InternalIPInterfaceAddressPrefix 192.168.137.0/24
```

**④ 验证**

```powershell
Get-NetNat          # 应看到 BoardNat / 192.168.137.0/24 / Active=True
```

板上 `ping 112.124.58.134` 和 `ping baidu.com` 都通即成功。

> [!important] WinNAT 没有 DHCP，也没有 DNS 代理
> 这是它和 ICS 最大的实操差别：ICS 会顺手给板子发地址、代答 DNS，**WinNAT 两样都不做**。
> 所以切过去之后：**板子必须静态 IP**（否则拿不到地址）、**DNS 必须指公网**（`223.5.5.5`）。
> 代价换成好处：少了两条依赖，不用再管 ICS 的 DHCP 租约和 DNS 代理有没有起来。
>
> 另外 **WinNAT 和 ICS 不能共存**，切之前必须先把共享关掉，否则 `New-NetNat` 会冲突。

> [!tip] 别拿 `Forwarding=Disabled` 判断 ICS 好坏
> `Get-NetIPInterface -AddressFamily IPv4` 里所有接口的 `Forwarding` 都是 `Disabled`，
> 包括 ICS 的下游网卡——**这是正常的**。ICS 靠 `ipnat.sys` 做地址转换，不走 IP 转发那个开关。
> 拿它判断好坏，会把正常的当故障。

> [!tip] DNS 坏了不影响 frpc，这是两条独立的线
> frpc 连的是 `112.124.58.134:7000`，纯 IP，不走 DNS。所以：
>
> - `ping 112.124.58.134` **不通** → 出网/NAT 问题，查本节
> - ping 通但 `ping baidu.com` **卡住** → 只是 DNS，查 13.4 的 nameservers
>
> 想确认是不是真没人答，在 PC 上跑一条就够：
>
> ```powershell
> Get-NetUDPEndpoint -LocalPort 53 | Select-Object LocalAddress,OwningProcess
> ```
>
> 输出为空 = 没有程序在 53 端口应答（ICS 的 DNS 代理没起来），板子自然解析不了。
> 另外 BusyBox 的解析超时可能十几秒，`ping 域名` 敲完别急着 `^C`，先用 `nslookup 域名` 看具体报错。

### 13.6 断电自检清单

板子会经常直接断电，恢复后按这个顺序查，哪一环断了一眼能看出来：

```bash
date                                   # 时间对不对（RTC 没电池会漂）
cat /sys/class/net/eth0/address        # MAC 是不是固定的那个
connmanctl services                    # 服务名对不对、是否 ready
ifconfig eth0 | grep inet              # IP 是不是 192.168.137.91
ping -c 2 112.124.58.134               # 出网。不通 → NAT 没建，看 13.5
nslookup baidu.com                     # DNS。上面通这里不通 → 只改 nameservers（13.4）
/etc/init.d/nginx status
/etc/init.d/frpc status
tail -3 /var/lib/frpc/keepalive.log
```

> [!info] NTP、ntpdate，以及这块板为什么一直校不了时
> **NTP = Network Time Protocol**（网络时间协议），走 **UDP 123**，客户端问服务器"现在几点"，
> 拿回一个带层级（stratum）的时间。**`ntpdate`** 是它的一次性客户端：问一次、设一次、退出，
> 不做平滑调整——适合开机校时；长期守时要用常驻的 `ntpd` / `chronyd`。
>
> 这块板的坑：`/usr/bin/ntpdate-sync`（一个包装脚本）第三行就是
> `test -x /usr/sbin/ntpdate || exit 0`，而 Yocto 镜像里**根本没有 `/usr/sbin/ntpdate`**。
> 于是它每次都静默退出、返回 0，看着像"校过了"，其实一次都没校成（日志时间停在几个月前就是这么来的）。
>
> 现在改用 `/usr/local/bin/board-time.sh`：不碰 NTP，直接读 HTTP 响应头里的 `Date`
> （服务器自己的 UTC 时间，精度 1 秒）。HTTP 一定通，UDP 123 却常被校园网/公司网挡掉。
> 对日志时间戳、证书有效期判断，1 秒误差完全够用。

**串口是最后的安全网**：`/etc/inittab` 里配了 `getty -l /bin/autologin`，串口 115200 接上就是 root shell，不用密码。网络彻底起不来时从这里进。

**真正验证自启**（只挂软链不算数，必须重启一次）：

```bash
sync                  # 把缓存写盘，防止断电丢数据
/sbin/reboot          # 必须绝对路径，PATH 里可能没有 reboot
```

等 30–60 秒重连，`uptime` 应该是 0–1 分钟。

**如果 frpc 没起来**：`tail -20 /var/lib/frpc/frpc.log`。看到 `network is unreachable` + `no additional retries`，就是第 12 步的 `loginFailExit = false` 没生效。

### 13.7 四个容易混淆的概念（Tips）

> [!question]- Q1 为什么第 7 节能用 systemctl，板子上却说不能用？
> 因为这是**两台不同的机器、两套不同的 init 系统**，不是同一个环境：
>
> | | 公网服务器（Ubuntu） | 开发板（Yocto） |
> | --- | --- | --- |
> | PID 1 | `systemd` | `/sbin/init.sysvinit` |
> | 自启方式 | `systemctl enable xxx` | `/etc/init.d/xxx` + `/etc/rc5.d/S*` 软链 |
> | 服务定义放哪 | `/etc/systemd/system/*.service` | `/etc/init.d/` 下的 shell 脚本 |
> | 改完配置要做什么 | `systemctl daemon-reload` | 什么都不用做，重启或手动 start |
>
> 第 7 节写的 `systemctl enable --now frps` 是给**服务器**装 frps 用的，服务文件是 `/etc/systemd/system/frps.service`。板子这边对应的是本节这套脚本。不要把服务器上的命令搬到板子上。
>
> 板子上实测（2026-11-24，逐条验证过）：
>
> ```bash
> systemctl                         # sh: systemctl: command not found
> find / -name "systemctl*"         # 无任何结果
> ls /lib/systemd /etc/systemd      # No such file or directory
> readlink -f /proc/1/exe           # /sbin/init.sysvinit
> busybox --list | grep -i system   # 无（连 busybox applet 都没有）
> ```

> [!question]- Q2 为什么「能 systemctl 但 daemon-reload 不能用」这种组合不存在？
> 因为 `daemon-reload` **不是独立命令**，它是 `systemctl` 的一个子命令：
>
> ```bash
> systemctl daemon-reload     # 真实写法，整条才是一个完整命令
> ```
>
> 所以不存在「`systemctl` 能用、`daemon-reload` 不能用」这种状态：要么两个都能用，要么 `systemctl` 这个文件根本不存在。
>
> 容易遇到的**相似现象**是另一种情况——装了 systemd 的包，但 PID 1 不是 systemd（比如手动装过 systemd，或从别的发行版拷了二进制）。这时 `systemctl` 文件存在、敲得动，但任何子命令（**包括** `daemon-reload`）会一起失败：
>
> ```
> System has not been booted with systemd as init system (PID 1). Can't operate.
> Failed to connect to bus: Host is down
> ```
>
> 这不是「daemon-reload 不支持」，而是它连不上 PID 1。这块板子连这个阶段都没有——`systemctl` 文件压根不存在。

> [!question]- Q3 daemon-reload 依赖 systemctl 吗？
> 依赖。关系是单向的：`daemon-reload` 是 `systemctl` 的子命令参数，没有 `systemctl` 就没有它。
>
> 它要做的事是「让 PID 1 重新读取磁盘上的 unit 文件」。实现方式是 `systemctl` 通过 D-Bus 连到 `/run/systemd/private` 这个 socket，请求 systemd 重载。板子上连 `/run/systemd` 目录都没有，所以无从谈起。

> [!question]- Q4 systemd 和 daemon-reload 是一回事吗？分别是什么？
> 不是一回事，而且不在同一个层级：
>
> | 名称 | 是什么 | 类比 |
> | --- | --- | --- |
> | **systemd** | 一套 init 系统：PID 1 那个程序本体，外加 journald、udev、timedated 等组件 | 发动机 + 控制中心 |
> | **systemctl** | systemd 的命令行客户端，通过 D-Bus 跟 PID 1 对话 | 方向盘 / 仪表盘 |
> | **daemon-reload** | `systemctl` 的一个子命令，只做一件事：让 systemd 重读 unit 文件 | 仪表盘上的一个按钮 |
>
> 问「systemd 和 daemon-reload 一样吗」，相当于问「发动机和仪表盘上的一个按钮一样吗」。

#### 结论：这套板子为什么不需要这些

sysvinit 没有「已加载的 unit 缓存」这个概念——`/etc/init.d/` 下的脚本是**开机时由 `rc` 逐个启动**的，`rc5.d` 里的软链也是每次启动现读。所以：

- 改了 `/etc/init.d/frpc` 或换了 `rc5.d` 软链 → **不需要任何 reload**，下次开机自动生效
- 想立刻生效 → 直接 `/etc/init.d/frpc start`（或 `restart`）
- 没有「注册」这一步，`enable` / `disable` / `daemon-reload` 这三个动作在 sysvinit 里全都不存在；挂软链本身就是「enable」，删软链就是「disable」


---

## 14. 全链路分层验证

**干什么**：从内往外一层层确认，哪层断了一眼能看出来。

**为什么**：直接开浏览器只会看到一个 502，不知道是板子、隧道还是证书的问题。
分层验证能迅速把范围缩到一层。

```bash
# 第 1 层：板子里，nginx 有没有在发文件
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/          # 期望 200

# 第 2 层：板子里，frpc 有没有连上
/etc/init.d/frpc status                                             # running
tail -3 /var/lib/frpc/frpc.log                                           # start proxy success

# 第 3 层：服务器里，有没有开出 8080
ss -lntp | grep 8080                                                # 127.0.0.1:8080
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/     # 200

# 第 4 层：本机，外网能不能访问
curl --noproxy '*' -s -o /dev/null -w "%{http_code}\n" https://board.nexuslink.icu/
```

**逐页检查**（本机 PowerShell，一次跑完 13 条路由）：

```powershell
foreach ($u in '/','/lab/','/project/','/course-design/','/docs/','/notes/','/source/','/gallery/','/status/','/about/','/rss.xml','/status.json','/favicon.svg') {
  $c = curl.exe --noproxy '*' -s -o NUL -w "%{http_code}" "https://board.nexuslink.icu$u"
  "$u -> $c"
}
```

全部 200 即通过。

> `--noproxy '*'` 是绕过本机代理。机器上配了代理的话，不加会得到假的 502。

**故障对应关系**

| 断在哪层 | 现象 | 去看什么 |
| --- | --- | --- |
| 第 1 层 | 板内 curl 不是 200 | `nginx -t`、`/var/log/nginx/error.log`、站点目录有没有文件 |
| 第 2 层 | frpc 不 running | `tail -20 /var/lib/frpc/frpc.log`，多半是网络或 token |
| 第 3 层 | 没有 8080 | 板子没连上（回第 2 层） |
| 第 3 层有 8080 但 502 | 板内 nginx 挂了 | 回第 1 层 |
| 第 4 层 | 外网 502 但第 3 层 200 | 隧道时断时续，看 frpc 日志有没有频繁重连 |

---

## 15. 确认证书能自动续期

**干什么**：模拟一次续期，确认 90 天后不会突然证书过期。

**为什么**：Let's Encrypt 证书只有 90 天。certbot 装了定时任务会自动续，
但要用 `--dry-run` 演练一次，才知道这个自动续真能跑通。

```bash
certbot renew --dry-run --no-random-sleep-on-renew
```

**怎么算成功**

```
Congratulations, all simulated renewals succeeded:
  /etc/letsencrypt/live/board.nexuslink.icu/fullchain.pem (success)
```

> 不加 `--no-random-sleep-on-renew` 会随机等最多 8 分钟（certbot 的错峰设计，
> 避免全世界的机器同时打 Let's Encrypt）。没输出别以为卡死，那是它在睡。

---

## 16. 日常更新（改完内容往上传）

```powershell
cd "D:\Documents\大三上\嵌入式系统\项目\embeded_web"
npx astro build
```

```bash
# Git Bash 里：预压缩 + 打包
cd dist
find . -type f \( -name '*.html' -o -name '*.css' -o -name '*.js' -o -name '*.xml' \
  -o -name '*.json' -o -name '*.svg' \) -exec gzip -9 -k -f {} \;
cd ..
tar czf site.tar.gz -C dist .

# 上传 + 替换
scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    site.tar.gz root@192.168.137.91:/tmp/
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.137.91 \
    'ls /var/www/site >/dev/null && rm -rf /var/www/site/* && tar xzf /tmp/site.tar.gz -C /var/www/site'
```

**不需要** `nginx -s reload`，也不需要重启 frpc——静态文件每次请求都从磁盘读。
只有改了 **nginx 配置文件**才需要 reload。

```bash
curl --noproxy '*' -s -o /dev/null -w "%{http_code}\n" https://board.nexuslink.icu/
```

---

## 附录 A · 备选方案 B（当前未启用）：不用 nginx，让 frpc 自己发文件

> **本节是对照和备用，不是主流程。** 线上跑的是方案 A——第 11 步配板上 nginx、
> 第 12 步让 frpc 转发给 127.0.0.1:80。只有板子上没有 nginx、也装不了的时候，才改用本节。

**什么时候用**：板子上没有 nginx，也装不了（没有包管理器、没法交叉编译）。

**原理**：frpc 有 `static_file` 插件，它自己就是极简 Web 服务器，直接读目录发文件。
省掉 nginx 这一层，但也丢掉 nginx 的能力。

**配置差异**：把 `/etc/frp/frpc.toml` 里 `[[proxies]]` 之后的部分换成：

```toml
[[proxies]]
name = "board-web"
type = "tcp"
remotePort = 8080

[proxies.plugin]
type = "static_file"
localPath = "/var/www/site"
```

**代价**（实测对比）

| | 方案 A（nginx） | 方案 B（static_file） |
| --- | --- | --- |
| 首页体积 | 6739B（gzip） | 27192B（不压缩） |
| 缓存头 | 有 `Cache-Control` | 无 |
| 404 | 站点自己的页面 | frp 纯文本 `404 page not found` |
| 进程 | nginx + frpc | 只有 frpc |
| 内存 | 约 11MB | 约 6MB |

**好处**：不依赖任何额外软件；换文件即时生效，连 reload 都不用。

---

## 附录 B · 两个方案互切

只改 `/etc/frp/frpc.toml`，然后 `/etc/init.d/frpc restart`，中断约 2 秒。

| 方向 | 改什么 | 前提 |
| --- | --- | --- |
| B → A | 删掉 `[proxies.plugin]` 段，加 `localIP = "127.0.0.1"` + `localPort = 80` | 板上 nginx 已跑通（`curl 127.0.0.1` 有 200） |
| A → B | 反向操作 | 站点文件在 `localPath` 下 |

**切换顺序很重要**：先把目标方案在本机端口上验证通过（`curl 127.0.0.1`）再改 frpc。
这样切换失败时线上只是短暂 502，回滚只需把配置改回去。

---

## 附录 C · 踩过的坑（每条都真踩过）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 装完 nginx 起不来，整机 80/443 全空 | 旧配置引用了不存在的证书 | 先只加载 80 段临时配置，签完证书再换正式配置（第 4 步） |
| 重启后站点 502，手动启动就好 | `S99frpc` 早于网络就绪，frpc 登录失败一次即退出 | 配置加 `loginFailExit = false` |
| `nginx: [emerg] INET6 sockets are not supported` | 板上 nginx 1.8.1 没编译 IPv6 | 删掉 `listen [::]:80` |
| 放了 site.conf 还是 nginx 默认页 | Yocto 的 nginx.conf 缺 `include conf.d/*.conf`，目录也不存在 | 补 include + `mkdir -p /etc/nginx/conf.d` |
| 页面没压缩（没有 `Content-Encoding`） | 缺 `.gz` 文件 | 重新预压缩（第 11.3 步） |
| `Cache-Control` 出现两条 | `expires` 和 `add_header` 同时生效 | 只留 `add_header` |
| `no matching host key type found. Their offer: ssh-rsa` | 板上 dropbear 只支持 `ssh-rsa`（SHA-1），本机 OpenSSH ≥ 8.8 默认禁用 | **每条** `ssh` / `scp` 都加 `-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa`；或在 `~/.ssh/config` 建 `Host board` 别号（§8） |
| `scp` 打印 `usage: scp [-346ABCOpqRrsTv] …` | PowerShell 里照抄了 bash 的 `\` 续行，`\` 被当成一个参数 | 去掉 `\` 写成一行（§13.1 有 PowerShell 版）；这不是密钥问题 |
| 手动启动 frpc 后站点还是 502，`status` 却显示 running | `status` 只查进程活着，不查隧道；常见是板子出不了网 | `tail -5 /var/lib/frpc/frpc.log` 看是 `i/o timeout` 还是 `login success`，再 `ping 112.124.58.134`（§13.1 有判读表） |
| frpc 启动即退出，日志说找不到 `./frpc.ini` | 配置路径用了位置参数 `frpc /etc/frp/frpc.toml`，被静默忽略 | 必须 `frpc -c /etc/frp/frpc.toml`（§12） |
| `nginx: command not found`（ssh 下） | 非登录 shell 的 PATH 不含 `/usr/sbin` | 用 `/usr/sbin/nginx` |
| `pkill -f 'frpc -c'` 无输出、退出码 0 | `-f` 匹配到执行命令的 shell 自身，把自己杀了 | 用 `/etc/init.d/frpc stop` |
| 外网能直连 `:8080` | `proxyBindAddr` 不是 `127.0.0.1` | 改回来，`systemctl restart frps` |
| 板子 IP 变了，SSH 连不上 | ICS 的 DHCP 重新分配 | 串口里 `ip -4 addr show eth0`，或本机 `arp -a` 按 MAC 找 |
| tar 提示 `time stamp ... in the future` | 板子 RTC 掉电，时钟停在几个月前 | `date -s` 校时，不影响文件内容 |
| `certbot renew --dry-run` 卡住没输出 | 随机睡最多 8 分钟 | 加 `--no-random-sleep-on-renew` |
| certbot 报 `DNS problem: NXDOMAIN` | A 记录没加或没生效 | `nslookup 域名 223.5.5.5` |
| 临时配置写完，`curl` 回 301 而不是 `acme-ok` | 只写在 `sites-available`，没 `ln -s` 到 `sites-enabled`；或建了没 reload | 建软链 → `nginx -t && reload`（第 4 步 ③） |
| 改了配置但行为没变 | 没 reload，进程还在用旧配置 | `nginx -T` 看磁盘、`curl` 看在跑的；`systemctl reload nginx` |
| GitHub 下载 frp 超时 | 境内直连慢 | 用 `https://ghfast.top/` 前缀 |
| 每次开机后板子能 `ping 192.168.137.1`，但出不了网，把 ICS 关掉再开一次就好 | ICS 的 NAT 表只在「开启共享那一刻」建一次，开机时 USB 网卡还没枚举完，表没建成 | 关开只是临时救急；换 WinNAT（§13.5） |
| `ping 112.124.58.134` 通，但 `ping baidu.com` 连 `PING ...` 那行都不打印 | 卡在 DNS 解析；ICS 的 DNS 代理（PC 的 53 端口）没起来 | 板子 DNS 改 `223.5.5.5`；PC 上 `Get-NetUDPEndpoint -LocalPort 53` 确认有没有人应答（§13.5） |
| 查 `Get-NetIPInterface` 发现 `Forwarding` 全是 `Disabled`，以为转发没开 | ICS 用 `ipnat.sys` 做地址转换，不走 IP 转发开关 | 这个值本来就是 Disabled，别当故障（§13.5） |
| 切到 WinNAT 后板子拿不到 IP | WinNAT 不提供 DHCP，也不做 DNS 代理 | 板子必须配静态 IP、DNS 指公网（§13.5 ①） |
| 点应用按钮提示「请求失败」，但「回桌面」正常 | 前端把 `app:camera` 编码成 `app%3Acamera`，后端没 `unquote` | 后端解析 query 时解码（§18.1） |
| 接口 502，日志里 `UnicodeDecodeError ... ascii` | 板上 locale 是 ASCII，读 cfg 的中文、或 `python3 -c` 里的中文注释炸了 | `open(..., encoding='utf-8')`；`-c` 代码块只用英文注释 |
| 应用退出后屏幕残留最后一帧 | agent 拉起的 app 不在 systemui 进程树里，它不会重绘 | 点「回桌面」（`a=desk`），§18.1 |
| `board-agent status` 说 running，但 `/agent/` 还是不通 | 只查进程，不查 nginx 反代 | `curl -s 127.0.0.1/agent/api/status` 分层看（§17.3） |

---

## 附录 D · 一页纸检查清单

| # | 检查 | 命令 / 位置 | 期望 |
| --- | --- | --- | --- |
| 1 | 域名解析 | `nslookup board.nexuslink.icu 223.5.5.5` | 服务器 IP |
| 2 | 安全组 | 阿里云控制台 | 80/443/7000 放行 |
| 3 | 服务器 nginx | `systemctl is-active nginx` | active |
| 4 | 证书 | `ls /etc/letsencrypt/live/board.nexuslink.icu/` | 四个 pem |
| 5 | frps | `systemctl is-active frps` | active |
| 6 | 7000 公网可达 | 本机 TcpClient 测 7000 | True |
| 7 | 板内 nginx | 板上 `curl 127.0.0.1` | 200 |
| 8 | 板内 gzip | 板上 `curl -H "Accept-Encoding: gzip"` | `Content-Encoding: gzip` |
| 9 | frpc | 板上 `/etc/init.d/frpc status` | running |
| 10 | 服务器 8080 | `ss -lntp \| grep 8080` | `127.0.0.1:8080` |
| 11 | 外网首页 | 浏览器 / curl | 200 |
| 12 | 全站 13 条路由 | 第 14 步的循环 | 全 200 |
| 13 | 重启自恢复 | 重启板子后再查第 9、11 项 | 均正常 |
| 14 | 证书续期 | `certbot renew --dry-run --no-random-sleep-on-renew` | succeeded |
| 15 | agent 在跑 | 板上 `curl -s 127.0.0.1:7681/api/status` | `"ok":true` |
| 16 | 状态经 nginx | `curl -s https://域名/agent/api/status` | JSON |
| 17 | WebSocket | `curl -i -N --http1.1 …`（17.5） | `101 Switching Protocols` |
| 18 | 首页在跳 | 浏览器看 uptime | 每秒变化 |
| 19 | PC 侧能出网 | 板上 `ping -c 2 112.124.58.134` | 有回包 |
| 20 | PC 侧 DNS | 板上 `nslookup baidu.com` | 返回 IP |
| 21 | 遥控接口 | 板上 `curl -H "X-Board-Token: $(cat /etc/board-agent.token)" 127.0.0.1:7681/api/apps` | JSON 列表 |
| 22 | 校时 | 板上 `date -u` | 与真实 UTC 相差 < 1 分钟 |

---

## 17. 板端 agent：把 shell 和屏幕搬进浏览器（新增）

> 这一步是在站点已经能打开之后加的，**不影响前 16 步**。
> 不装它，站点照常是纯静态站；装了它，首页的数字才会真的跳，
> `/console/` 也才连得上。

### 17.0 多出来的是什么东西

一个**单文件、纯标准库**的 Python 进程 `board-agent.py`，同时干三件事：

| 路径 | 协议 | 干什么 |
| --- | --- | --- |
| `/agent/api/status` | HTTP | 读 `/proc` `/sys`，返回 JSON 遥测（首页每 2s 取一次） |
| `/agent/ws/shell` | WebSocket | 开一个 PTY，把 `bash -i` 的输入输出双向搬运 |
| `/agent/ws/fb` | WebSocket | 读 `/dev/fb0`，只推**变化的 64×64 块** |

它不依赖任何第三方库，也不需要交叉编译 —— 板上自带 Python 3.5 就够了。
（`pty` 模块在 Yocto 的裁剪里被砍了，但 `os.openpty()` 还在，够用。）

### 17.1 链路：没有新开任何公网端口

```
浏览器
  │  wss://board.nexuslink.icu/agent/ws/fb
  ▼
阿里云 nginx :443  ← 这里必须透传 Upgrade（很多人的坑就踩在这）
  │  127.0.0.1:8080（frps）
  ▼
frp 隧道
  │
板子 nginx :80
  │  location /agent/ → proxy_pass 127.0.0.1:7681
  ▼
board-agent.py  127.0.0.1:7681   ← 只绑回环，公网永远看不到
```

**为什么 agent 不直接让 frpc 转发一个新端口？**
也可以（8081 在 `allowPorts` 范围内），但那样服务器 nginx 和 frps 都要改；
挂在板上 nginx 的 `/agent/` 前缀下，**服务器只加一个 location，安全组一个都不用动**。

### 17.2 板子：装 agent 并前台试跑

**干什么**：把脚本放上去，跑起来，拿到 token。

```bash
# 本机（Git Bash）
scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    deploy/board-agent.py root@192.168.137.91:/usr/local/bin/
```

```bash
# 板上
chmod +x /usr/local/bin/board-agent.py
python3 /usr/local/bin/board-agent.py -v        # 前台跑，先别 Ctrl+C
```

另一个终端（或再开一个 ssh）：

```bash
curl -s http://127.0.0.1:7681/api/status | head -c 300
cat /etc/board-agent.token                      # 24 字符，首次启动自动生成
```

**怎么算成功**：`/api/status` 返回一大坨 JSON，里面有 `"ok":true`、`"temp":48.9`、
`"nginx":{"ver":"1.8.1","workers":1}`。

> token 文件权限是 600、属主 root。它等价于这块板子的 root 密码，
> **不要**提交进仓库、不要贴给别人、不要写进前端代码。

停掉前台进程（`Ctrl+C`），下面改用后台跑：

```bash
nohup python3 /usr/local/bin/board-agent.py >>/var/lib/board-agent/agent.log 2>&1 &
echo $! > /var/run/board-agent.pid
```

### 17.3 板子：nginx 加两个 location

**干什么**：把 `/agent/api/` 与 `/agent/ws/` 反代到 7681。

**为什么分两段**：WebSocket 需要透传 `Upgrade`/`Connection` 两个头，
普通接口不需要；分开写最省事，也避免把接口请求也标成 upgrade。

把这两段加进 `/etc/nginx/conf.d/site.conf` 的 `server { }` 里（`location /` 之前）：

```nginx
    # 状态 API：普通 HTTP
    location /agent/api/ {
        proxy_pass         http://127.0.0.1:7681;
        proxy_http_version 1.1;
        proxy_set_header   Host            $host;
        proxy_set_header   X-Real-IP       $remote_addr;
        proxy_read_timeout 15s;
        proxy_buffering    off;
        add_header         Cache-Control "no-store";
    }

    # WebSocket：shell 与屏幕推流
    location /agent/ws/ {
        proxy_pass         http://127.0.0.1:7681;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering    off;
    }
```

```bash
/usr/sbin/nginx -t && /usr/sbin/nginx -s reload
curl -s http://127.0.0.1/agent/api/status | head -c 120
```

> 文件版的完整配置见仓库 `deploy/nginx.conf`，直接覆盖也行：
> ```bash
> scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
>     deploy/nginx.conf root@192.168.137.91:/etc/nginx/conf.d/site.conf
> ```

**怎么算成功**：curl 返回 JSON，而不是站点自己的 404 页面。

### 17.4 服务器：443 那段的 Upgrade 头

**为什么必须改**：请求要穿过**两层** nginx。板上那层已经配好了，
服务器这层如果只写 `proxy_pass`，nginx 会把 `Connection: upgrade` 当成逐跳头丢掉，
于是 WebSocket 握手在浏览器里拿到的是 `200` 而不是 `101`，
表现就是「点了连接没反应 / 立刻断开」。

在 `/etc/nginx/sites-available/board` 的 443 `server { }` 里，`location /` **之前**加：

```nginx
    location /agent/ws/ {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering    off;
    }
```

```bash
nginx -t && systemctl reload nginx
```

> 完整文件见仓库 `deploy/nginx-server.conf`。
> 改之前建议先 `cp -a /etc/nginx/sites-available/board /root/board.conf.bak`。

### 17.5 浏览器：三个地方验收

1. **首页** —— 右上角终端卡片的 `uptime` 每秒在走，`memory`、`load / temp` 每 2 秒变一次，
   底部的「每 2s 采样 N 次」计数在涨；再往下「实时遥测」四张折线图会一点点长出来。
2. **`/console/` → Shell 终端** —— 粘贴 token，回车，出现 `root@board:` 提示符。
   敲 `top`、`free -m`、`ls /dev` 都行，跟串口看到的一样。
3. **`/console/` → 远程桌面** —— 2 秒内出现那块 1024×600 的屏幕（板上跑着 Qt 启动器）。
   静止时「已收」不再增长；整分钟跳表时会多出一两个 8KB 的块。

**怎么算成功**：

```bash
# 用 curl 看握手是不是 101（把 TOKEN 换成真实 token）
curl -i -N --http1.1 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Protocol: board.v1, $TOKEN" \
  https://board.nexuslink.icu/agent/ws/shell | head -5
# 期望第一行：HTTP/1.1 101 Switching Protocols
```

### 17.6 开机自启（可选，放在第 13 步之后做）

板子是 sysvinit，写法跟 frpc 一样：脚本进 `/etc/init.d/`，再挂 `rc5.d` 软链。

```bash
# 仓库里已有现成的 deploy/board-agent.init（比内联版多了 restart、python 路径探测）
scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    deploy/board-agent.init root@192.168.137.91:/etc/init.d/board-agent
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.137.91 '
  chmod 755 /etc/init.d/board-agent
  ln -sf /etc/init.d/board-agent /etc/rc5.d/S98board-agent
  ln -sf /etc/init.d/board-agent /etc/rc0.d/K02board-agent
  ln -sf /etc/init.d/board-agent /etc/rc6.d/K02board-agent
  /etc/init.d/board-agent restart
  /etc/init.d/board-agent status
'
```

脚本只认四个动作：`start` / `stop` / `restart` / `status`。`rc` 只会用前两个
（`S98` 传 `start`，`K02` 传 `stop`）。

> `S98` 排在 `S92nginx` 之后、`S99frpc` 之前都可以；agent 不依赖外网，
> 只依赖本机回环，所以先后无所谓。
>
> 第 13.2 步的 `board-keepalive.sh` 已经带上 board-agent 的巡检
> （用 `[ -x /etc/init.d/board-agent ]` 做守卫，没装就跳过），所以装完这步就自动被守护。

### 17.7 代价与安全

| 项 | 实测 |
| --- | --- |
| agent 常驻内存 | 约 9–10 MB RSS（Python 解释器本身就占 6MB） |
| 首页 2s 轮询的开销 | 每次请求约 4–6ms CPU，读 `/proc` 不落盘 |
| 屏幕静止时的带宽 | 0（除 TCP 心跳），整分钟跳表约 8KB |
| 首帧 | 1.2MB，公网约 0.4–0.8s |
| 推流时 CPU | `interval=0.8s` 下约 **29%** 单核（整帧快速比较生效后）；改前逐块比较是 51% |
| 帧率上限 | 本机约 **4.3 fps** —— 读 `/dev/fb0` 1.2MB 要 221ms，整帧 memcmp 9ms，全是硬开销 |
| 遥控一个应用 | 约 0.2s（`exec` 后画面由推流带过来）；「回桌面」约 2s（要重启启动器） |
| 终端按键 | 几十字节/次，延迟与 ssh 相当 |

**安全上做了三件事**：

1. agent 只监听 `127.0.0.1`，公网必须经过 nginx 的 `/agent/` 才能进；
2. shell 与屏幕都要求 token，且 token **放在 `Sec-WebSocket-Protocol` 子协议里**，
   不放 URL —— 否则会被 nginx 的 access_log 原样记下来；
3. 鉴权失败先 `sleep 0.6s` 再返回 401，把在线爆破的速度压到每秒一次以内。

> 仍然要清楚：**这是一个公网可达的 root shell**。token 泄露等于板子被拿走。
> 想更稳可以再叠一层：给 `/agent/` 加 `allow/deny` 白名单，
> 或者用 `auth_basic` 加一道 nginx 密码（记得只加在 `/agent/` 上，别把整站锁住）。

**桌面为什么只能看不能操作**：这块板子的 GUI 是 `/opt/ui/systemui`，用 Qt Quick 的
`linuxfb` 插件直接画到 `/dev/fb0`（`QT_QPA_PLATFORM=linuxfb:tty=/dev/fb0`），不经过 X11；内核也**没有编 uinput 模块**（`grep uinput /proc/devices` 无输出），
所以造不出虚拟输入设备，注入鼠标这件事在驱动层就不成立。
想操作只有两种程度，见第 18 节：

- **指令级遥控**（18.1）：直接 exec 应用，**稳定、已做进页面**，但只能"开应用"，不能点任意位置；
- **触摸注入**（18.4）：在应用读设备那层开侧门（LD_PRELOAD 拦 `poll`/`read`），
  能做到"点哪打哪"，但**不稳定、默认关闭**。

### 17.8 排错表

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `/agent/api/status` 返回站点 404 页 | location 没生效 / 没 reload | `nginx -T \| grep -n "agent"`，再 `-s reload` |
| 浏览器控制台 `WebSocket is closed before the connection is established` | 服务器那层没透传 Upgrade | 回到 17.4 补 `location /agent/ws/` |
| 401 | token 不对 / 带了多余空格 | `cat /etc/board-agent.token` 重新粘贴 |
| 连上后 1 秒就断 | 某层 `proxy_read_timeout` 太短 | 两处都写 3600s |
| 终端能连但不回显 | PTY 起不来（`/bin/bash` 不在） | agent 日志里会有 `shell 异常` |
| 桌面全黑 | 首帧还没到（1.2MB） | 等 1–2 秒；或点「整屏刷新」 |
| 桌面卡住不动 | 屏上确实没变化 | 看「最近更新」时间；整屏刷新试一次 |
| agent 日志写不进去 | 板子 locale 是 ASCII，中文报错 | 已改成按 UTF-8 写字节，旧版本才有这个问题 |

---

## 18. 远程桌面的「交互」：遥控可用，触摸注入搁置

> 结论先放：**能用的那套是「指令级遥控」**（18.1，稳定，已经做进页面）。
> **「真·触摸注入」**——点画面任意位置等于点板子——技术上摸到了，但不稳定，默认关闭（18.4 起）。
>
> 两条路的区别一句话：遥控是**命令板子开某个应用**；注入是**假装有根手指在按屏幕**。

### 18.1 指令级遥控：让板子自己启动应用（已实现，稳定）

**能做什么**：`/console/` 的远程桌面下方多了一排按钮——相机 / 音乐 / 照片 / 视频 / 位置 / 设置 /
传感器 / 蜂鸣器 / 测试 / LED / 按键——点一下，板子的屏幕真的切到那个应用；点「回桌面」回到启动器。

**为什么叫「指令级」**：它不是模拟手指点屏幕，而是**直接让系统执行那个程序**
（`/opt/ui/src/apps/<名字>`，启动器自己也是这么干的）。所以它 100% 可靠，
代价是没有鼠标指针，只能"开应用"，不能点画面里的任意按钮。

**三个接口**（都要 `X-Board-Token` 请求头）：

| 接口 | 作用 |
| --- | --- |
| `/agent/api/apps` | 解析启动器的 `apk1.cfg` / `apk2.cfg`，返回 `[{id, name}]` |
| `/agent/api/ctl?a=app:camera` | `exec ./apps/camera`，全屏接管屏幕 |
| `/agent/api/ctl?a=desk` | 杀掉在跑的应用 + 重启 `systemui` |

**一个绕不过的限制（实测）**：从 agent 拉起的 app 不在 `systemui` 的进程树里，
它退出后 `systemui` 不会重绘，屏幕会残留 app 的最后一帧。
所以"回桌面"只能是「杀应用 + 重启启动器」，约 2 秒，屏幕会闪一下黑。

> [!tip] 应用名是白名单
> `launch_app()` 先拿 `list_apps()` 的结果做校验，传 `app:../../bin/sh` 会返回 `unknown app`。
> 挡住的是"把名字拼进路径"这个口子。

> [!warning] action 里的冒号会被前端编码
> 前端 `encodeURIComponent('app:camera')` 得到 `app%3Acamera`，后端必须 `unquote` 一次，
> 否则匹配不上 `app:` 前缀，页面表现是**「请求失败」**。
> 而 `a=desk` 没有特殊字符，编码前后一样，所以它反而是好的 ——
> 很容易误判成"后端挂了"。这个坑我已经踩过一次。

**列表从哪来**：不是硬编码，是解析板上的
`/opt/ui/src/ATK-IMX6U/apk1.cfg`（9 个）和 `apk2.cfg`（2 个），
每行格式 `appicons/camera.png 相机 camera`（图标 / 中文名 / 程序名）。
启动器改了配置，页面刷新就跟着变。

> [!note] 为什么读 cfg 时必须显式 utf-8
> 板上默认 locale 是 ASCII，`open(path)` 读到中文会抛
> `UnicodeDecodeError: 'ascii' codec can't decode byte 0xe7`。
> 必须写 `open(path, 'r', encoding='utf-8', errors='replace')`。
> 同一个坑还坑过 `python3 -c`：代码里带中文注释也会炸。

### 18.2 为什么标准做法不行（都有实测证据）

| 标准做法 | 实测结果 |
| --- | --- |
| `/dev/uinput` 造虚拟触摸设备 | `/proc/config.gz` 里 `# CONFIG_INPUT_UINPUT is not set`，内核根本没编 |
| 加载 `uinput.ko` 模块 | `/lib/modules/4.1.15-ge48931b1/` 下没有任何 `uinput*`；板上也没有 `modprobe`/`insmod` |
| 往 `/dev/input/event1` 写事件 | evdev 是只读队列，内核不支持用户态写 |
| 给 tslib 喂一个 FIFO | tslib 用 `ioctl(EVIOCGVERSION)` 校验 fd，直接报 `Selected device is not a Linux input event device` |
| X11 方案（x11vnc / xdotool） | 板子没跑 X，GUI 是 Qt Quick + `linuxfb`，四个 `event*` 由它独占 |

### 18.3 这根板子的 GUI 到底怎么读触摸（这一段是关键）

排查过程中有三条容易踩空的结论，都验证过：

1. **`QT_QPA_FB_TSLIB=1` 是设着的，但 tslib 实际没在用。**
   把 `/usr/lib/ts/input.so` 换成带日志的版本后，`input_mod_init` 被调用了，
   但 `ts_read()` **一次都没被调用** —— 说明 Qt 只是把 libts 链进来了，
   真正读设备的是 `libqlinuxfb.so` 内置的 evdev 触摸。
2. **它读设备的方式是「poll 等 → read 16 字节」。**
   用 LD_PRELOAD 探针记录 `poll()` 的 fd 集合，能看到
   `fd 9 -> /dev/input/event1`；主线程 `wchan` 是 `poll_schedule_timeout`。
3. **同一个设备被进程内打开了两次**（fd 9 和 fd 12 都指向 event1），
   这是后面"时灵时不灵"的根源。

### 18.4 试出来的可行方案：在 poll/read 上开侧门

思路：不改内核、不动 Qt，只在**应用读设备**这一步加一个"第二来源"。

- `touch-inject.so`（源码在 `deploy/touch/touch-inject.c`）通过 `LD_PRELOAD` 注入到 systemui
- 拦截 `poll()/ppoll()`：如果等待集合里有触摸设备，就把 agent 的 unix socket 也加进去等；
  socket 有数据时，把设备 fd 标成可读，让应用去 `read`
- 拦截 `read()`：如果读的是触摸设备且 socket 里有事件，就返回这条事件
- 物理触摸完全不受影响；agent 不在时这个 .so 等于不存在

编译（在 x86 Ubuntu 上交叉编译，只依赖 GLIBC 2.4）见 `deploy/touch/build-shim.sh`。
注意两个坑：

- 必须 `-D_TIME_BITS=32`，否则 Ubuntu 24.04 的 armhf 头文件会把
  `struct input_event` 变成 24 字节（板子是 16 字节）
- 不能用 `dlsym(RTLD_NEXT)`，`dlsym` 在 glibc 2.34 才并进 libc，板子是 2.23；
  一律走 `syscall()` 直接调

**实测结果**：注入成功过 —— 点 (905,150) 真的打开了板子上的「设置」，
长按 (130,150) 选中了「正点原子」条目。

**但**：同样的操作重复几次就会失效。抓日志看到应用确实把这 8~14 条事件
从 fd 9 `read` 走了，界面却不动；而同一个设备还有第二个 fd（12）也在被读。
推测是非 UI 的那一路把事件吃掉了，或者 Qt 的 evdev 处理器按设备能力表
（只上报 `ABS_MT_*`，没有 `ABS_X/Y`）过滤掉了单点事件。

因为定位到这里需要继续动板子的输入栈，而且失败模式对演示不友好，
**默认没有启用**，代码与结论留在这里，谁要接着做可以照着走。

### 18.5 想自己启用触摸注入的话（三步，可回滚）

```bash
# ① 放 shim（已编译好的在 deploy/touch/touch-inject.so）
scp -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    deploy/touch/touch-inject.so root@192.168.137.91:/usr/local/lib/

# ② 让 GUI 带上 LD_PRELOAD 启动
sed -i 's#^/opt/ui/systemui#LD_PRELOAD=/usr/local/lib/touch-inject.so /opt/ui/systemui#' /etc/rc.local
# 立即生效（不重启板子）：
kill $(ps | grep -v grep | grep systemui | awk '{print $1}')
sh -c '. /etc/profile; LD_PRELOAD=/usr/local/lib/touch-inject.so nohup /opt/ui/systemui >/dev/null 2>&1 &'

# ③ 打开网页 /console/ → 远程桌面 → 点「只读」切成「可交互」
```

命令行也能验证（不需要浏览器）：

```bash
TOKEN=$(cat /etc/board-agent.token)
curl -s -H "X-Board-Token: $TOKEN" "http://127.0.0.1/agent/api/tap?x=905&y=150"
# 返回 {"ok":true,"connected":true,...} 表示事件已经送进 Qt 的 read 路径
```

**回滚**（这是推荐状态）：

```bash
cp /etc/rc.local.bak /etc/rc.local          # 备份在第一次修改时生成
kill $(ps | grep -v grep | grep systemui | awk '{print $1}')
sh -c '. /etc/profile; nohup /opt/ui/systemui >/dev/null 2>&1 &'
# 或者直接 reboot
```

### 18.6 如果还要继续做触摸注入，建议的下一步

1. **确定哪个 fd 才是真正的处理器**：在 shim 里统计每个触摸 fd 的 read 次数与
   poll 出现次数，找出被 event loop 长期等待的那一个，只给它投递。
2. **按设备能力表发事件**：设备只上报 `ABS_MT_*`，就发完整的 MT 协议 B 一帧
   （SLOT → TRACKING_ID → POSITION_X/Y → TOUCH_MAJOR → SYN），不要混发单点。
3. **时间戳**：内核给的 `input_event.time` 是开机后的单调时间，注入时最好跟着
   板子的 `CLOCK_MONOTONIC` 走，而不是墙上时间。

这三条做完，大概率能稳定下来；但都要在板子上反复重启 GUI 验证，属于"能动系统"的改动。
