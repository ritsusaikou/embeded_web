#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
board-agent.py —— i.MX6ULL 板端轻量代理（纯标准库，Python 3.5+，无需交叉编译）

一个进程同时提供三件事：

    GET /api/status   首页遥测：uptime / 负载 / 内存 / 温度 / 磁盘 / 网络 / 进程
    WS  /ws/shell     WebSocket ↔ PTY：浏览器里直接拿到板子的 root shell
    WS  /ws/fb        WebSocket 推 framebuffer 增量块：浏览器 canvas 还原成屏幕

设计约束（都是被 512MB 内存 / 单核 A7 / SD 卡逼出来的）：

    * 不引入任何第三方库，不 fork 外部进程，不往 SD 卡写状态文件
    * 只监听 127.0.0.1，由板上 nginx 以 /agent/ 前缀反代进来；
      公网侧永远看不到 7681 端口
    * shell 与屏幕都要求 token：token 放在 WebSocket 子协议里带，
      不放 URL（避免被 nginx access_log 记下来）
    * 屏幕只推"变化的 64×64 块"，静止画面带宽为 0

启动：

    python3 board-agent.py            # 默认 127.0.0.1:7681
    python3 board-agent.py -p 7681 -v

token 存放：/etc/board-agent.token（首次启动自动生成，权限 600）
"""

import base64
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import select
import signal
import subprocess
import socket
import socketserver
from urllib.parse import unquote
import struct
import sys
import termios
import threading
import time
import traceback

VERSION = '1.0.0'
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
SUB_PROTO = 'board.v1'

HOST = '127.0.0.1'
PORT = 7681
TOKEN_FILE = '/etc/board-agent.token'
LOG_FILE = '/var/lib/board-agent/agent.log'
FB_DEV = '/dev/fb0'

TILE = 64                 # 屏幕分块边长，64×64×2B = 8KB/块
ANSI = re.compile(r'\x1b\[[0-9;]*m')
FB_DEFAULT_INTERVAL = 0.8  # 秒/帧
LOG_MAX = 256 * 1024

START_TS = time.time()
REQUESTS = [0]
REQ_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
def log(msg):
    # 板子默认 locale 是 ASCII，中文直接 write 会抛 UnicodeEncodeError，
    # 所以这里一律按 UTF-8 写字节，不依赖环境变量
    line = '%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    data = line.encode('utf-8', 'replace')
    with LOG_LOCK:
        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX:
                with open(LOG_FILE, 'rb') as f:
                    f.seek(-LOG_MAX // 2, os.SEEK_END)
                    tail = f.read()
                with open(LOG_FILE, 'wb') as f:
                    f.write(tail)
            d = os.path.dirname(LOG_FILE)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except Exception:
            pass
    if VERBOSE:
        try:
            sys.stderr.buffer.write(data)
            sys.stderr.flush()
        except Exception:
            pass


VERBOSE = False


# --------------------------------------------------------------------------
# token
# --------------------------------------------------------------------------
def load_token():
    t = os.environ.get('BOARD_TOKEN')
    if t:
        return t.strip()
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
            if t:
                return t
    except IOError:
        pass
    t = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip('=')
    try:
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, (t + '\n').encode())
        os.close(fd)
    except Exception as e:
        log('!! 写 token 文件失败: %s' % e)
    return t


# --------------------------------------------------------------------------
# /api/status 采集
# --------------------------------------------------------------------------
def _read(path, default=''):
    try:
        with open(path, 'rb') as f:
            return f.read().decode('utf-8', 'replace')
    except Exception:
        return default


def _procs():
    """扫描 /proc，统计进程名。返回 (总进程数, {name: count})"""
    names = {}
    total = 0
    try:
        for pid in os.listdir('/proc'):
            if not pid.isdigit():
                continue
            total += 1
            try:
                with open('/proc/%s/comm' % pid) as f:
                    n = f.read().strip()
                names[n] = names.get(n, 0) + 1
            except Exception:
                pass
    except Exception:
        pass
    return total, names


def _net_bytes():
    rx = tx = 0
    for line in _read('/proc/net/dev').splitlines():
        if ':' not in line:
            continue
        iface, data = line.split(':', 1)
        iface = iface.strip()
        if iface != 'eth0':
            continue
        f = data.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    return rx, tx


def _cpu_ticks():
    """返回 (idle, total) 累计 jiffies"""
    f = _read('/proc/stat').splitlines()
    if not f:
        return 0, 0
    p = f[0].split()
    vals = [int(x) for x in p[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)


def _meminfo():
    d = {}
    for line in _read('/proc/meminfo').splitlines():
        k, _, v = line.partition(':')
        try:
            d[k.strip()] = int(v.split()[0])
        except Exception:
            pass
    return d


def _temp():
    raw = _read('/sys/class/thermal/thermal_zone0/temp').strip()
    try:
        return round(int(raw) / 1000.0, 1)
    except Exception:
        return None


def _tunnel(names):
    """frpc 在跑 + 日志里最近一次是 start proxy success，才算真正连通"""
    if names.get('frpc', 0) < 1:
        return {'up': False, 'state': 'frpc 未运行', 'since': None}
    tail = _read('/var/lib/frpc/frpc.log')
    lines = [ANSI.sub('', ln) for ln in tail.splitlines()[-80:]]   # frpc 日志带颜色码
    ok_at = None
    state = '未知'
    for ln in lines:
        if 'start proxy success' in ln:
            ok_at = ln[:19]
            state = '已连通'
        elif 'login to server success' in ln:
            state = '已登录，等代理'
        elif 'connect to server error' in ln or 'login to server failed' in ln:
            state = '重连中'
            ok_at = None
    return {'up': bool(ok_at), 'state': state, 'since': ok_at}


_cpu_prev = {'idle': 0, 'total': 0, 'ts': 0}
_cpu_prev['idle'], _cpu_prev['total'] = _cpu_ticks()   # 先采一次，首个请求才有增量可算
_cpu_prev['ts'] = time.time()


def _cpu_percent():
    idle, total = _cpu_ticks()
    p = _cpu_prev
    pct = None
    if p['total'] and total > p['total']:
        dt = total - p['total']
        pct = round(100.0 * (1 - float(idle - p['idle']) / dt), 1)
    p['idle'], p['total'], p['ts'] = idle, total, time.time()
    return pct


def collect_status():
    up = 0.0
    try:
        up = float(_read('/proc/uptime').split()[0])
    except Exception:
        pass
    la = _read('/proc/loadavg').split()
    mem = _meminfo()
    mt = mem.get('MemTotal', 0)
    ma = mem.get('MemAvailable', mem.get('MemFree', 0))
    used = mt - ma
    total_proc, names = _procs()
    rx, tx = _net_bytes()
    try:
        st = os.statvfs('/')
        disk_total = st.f_blocks * st.f_frsize
        disk_free = st.f_bavail * st.f_frsize
    except Exception:
        disk_total = disk_free = 0
    uname = os.uname()
    return {
        'ok': True,
        'ver': VERSION,
        'ts': int(time.time()),
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'host': uname.nodename,
        'arch': uname.machine,
        'kernel': uname.release,
        'os': 'Yocto / i.MX6ULL',
        'uptime': int(up),
        'uptime_h': fmt_uptime(up),
        'load': [float(x) for x in la[:3]] if len(la) >= 3 else [0, 0, 0],
        'cpu': _cpu_percent(),
        'temp': _temp(),
        'mem': {
            'total': mt // 1024,
            'used': used // 1024,
            'free': mem.get('MemFree', 0) // 1024,
            'avail': ma // 1024,
            'pct': round(100.0 * used / mt, 1) if mt else 0,
        },
        'disk': {
            'total': disk_total // (1024 * 1024),
            'free': disk_free // (1024 * 1024),
            'pct': round(100.0 * (disk_total - disk_free) / disk_total, 1) if disk_total else 0,
        },
        'net': {'rx': rx, 'tx': tx},
        'procs': total_proc,
        'nginx': {
            'running': names.get('nginx', 0),
            'workers': max(0, names.get('nginx', 0) - 1),
            'ver': NGINX_VER,
        },
        'tunnel': _tunnel(names),
        'agent': {
            'pid': os.getpid(),
            'uptime': int(time.time() - START_TS),
            'requests': REQUESTS[0],
            'fb': os.path.exists(FB_DEV),
            'fb_size': '%dx%d' % (FB_W, FB_H),
            'touch': len(INJECT.conns) > 0,
            'touch_events': INJECT.sent,
        },
    }


def fmt_uptime(sec):
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return '%dd %02d:%02d' % (d, h, m)
    return '%02d:%02d' % (h, m)


NGINX_VER = 'nginx'
try:
    import subprocess  # 只在启动时用一次，python3.5 有

    out = subprocess.Popen(['/usr/sbin/nginx', '-v'], stderr=subprocess.PIPE,
                           stdout=subprocess.PIPE).communicate()[1].decode()
    if 'nginx/' in out:
        NGINX_VER = out.strip().split('nginx/')[-1]
except Exception:
    pass


# --------------------------------------------------------------------------
# framebuffer
# --------------------------------------------------------------------------
FB_W, FB_H, FB_BPP, FB_STRIDE = 0, 0, 16, 0


def fb_geometry():
    global FB_W, FB_H, FB_BPP, FB_STRIDE
    try:
        vs = _read('/sys/class/graphics/fb0/virtual_size').strip()
        FB_W, FB_H = [int(x) for x in vs.split(',')]
    except Exception:
        FB_W, FB_H = 1024, 600
    try:
        FB_BPP = int(_read('/sys/class/graphics/fb0/bits_per_pixel').strip())
    except Exception:
        FB_BPP = 16
    try:
        FB_STRIDE = int(_read('/sys/class/graphics/fb0/stride').strip())
    except Exception:
        FB_STRIDE = FB_W * FB_BPP // 8


# --------------------------------------------------------------------------
# WebSocket 基础
# --------------------------------------------------------------------------
def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def ws_send(sock, opcode, payload):
    n = len(payload)
    b1 = 0x80 | opcode
    if n < 126:
        head = struct.pack('>BB', b1, n)
    elif n < 65536:
        head = struct.pack('>BBH', b1, 126, n)
    else:
        head = struct.pack('>BBQ', b1, 127, n)
    sock.sendall(head)
    if n:
        sock.sendall(payload)


def _recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError('peer closed')
        buf += chunk
    return buf


def ws_recv(sock):
    b1, b2 = struct.unpack('>BB', _recv_exact(sock, 2))
    op = b1 & 0x0F
    masked = bool(b2 & 0x80)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack('>H', _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack('>Q', _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else None
    data = _recv_exact(sock, n) if n else b''
    if mask:
        data = bytes(bytearray([data[i] ^ mask[i & 3] for i in range(len(data))]))
    return op, data


# --------------------------------------------------------------------------
# /ws/shell
# --------------------------------------------------------------------------
def set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    except Exception:
        pass


def serve_shell(handler, rows, cols):
    sock = handler.connection
    master, slave = os.openpty()
    set_winsize(master, rows, cols)

    pid = os.fork()
    if pid == 0:
        try:
            os.close(sock.fileno())
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            if slave > 2:
                os.close(slave)
            try:
                os.close(master)
            except Exception:
                pass
            env = dict(os.environ)
            env.update({
                'TERM': 'xterm-256color',
                'HOME': '/root',
                'USER': 'root',
                'LANG': 'C.UTF-8',
                'PS1': '\\[\\e[38;5;208m\\]root@board\\[\\e[0m\\]:\\w# ',
            })
            os.execvpe('/bin/bash', ['bash', '--noprofile', '--norc', '-i'], env)
        except Exception:
            os._exit(1)
    os.close(slave)

    log('shell: 新会话 pid=%d %dx%d' % (pid, cols, rows))
    try:
        last_ping = time.time()
        while True:
            try:
                r, _, _ = select.select([master, sock], [], [], 5.0)
            except (OSError, select.error):
                break
            if master in r:
                try:
                    data = os.read(master, 8192)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    data = b''
                if not data:
                    break
                ws_send(sock, 0x2, data)
            if sock in r:
                op, data = ws_recv(sock)
                if op == 0x8:
                    break
                if op == 0x9:
                    ws_send(sock, 0xA, data)
                    continue
                if op == 0x1:
                    try:
                        msg = json.loads(data.decode('utf-8', 'replace'))
                    except Exception:
                        continue
                    t = msg.get('t')
                    if t == 'i' and msg.get('d'):
                        os.write(master, msg['d'].encode('utf-8'))
                    elif t == 'r':
                        cols = max(20, min(400, int(msg.get('c', cols))))
                        rows = max(6, min(200, int(msg.get('r', rows))))
                        set_winsize(master, rows, cols)
                    elif t == 'p':
                        ws_send(sock, 0xA, b'')
            if time.time() - last_ping > 15:
                ws_send(sock, 0x9, b'')
                last_ping = time.time()
    except (EOFError, OSError):
        pass
    except Exception:
        log('shell 异常: ' + traceback.format_exc().replace('\n', ' | '))
    finally:
        try:
            os.kill(pid, signal.SIGHUP)
        except Exception:
            pass
        try:
            os.close(master)
        except Exception:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass
        log('shell: 会话结束 pid=%d' % pid)


# --------------------------------------------------------------------------
# /ws/fb —— 只推变化的 64×64 块
# --------------------------------------------------------------------------
def serve_fb(handler, interval):
    sock = handler.connection
    if not os.path.exists(FB_DEV):
        ws_send(sock, 0x1, json.dumps({'t': 'err', 'm': '无 /dev/fb0'}).encode())
        return
    fd = os.open(FB_DEV, os.O_RDONLY)
    w, h, stride = FB_W, FB_H, FB_STRIDE
    frame_bytes = stride * h
    prev = None
    log('fb: 开始推流 %dx%d interval=%.2fs' % (w, h, interval))
    sent_frames = 0
    sent_bytes = 0
    try:
        while True:
            t0 = time.time()
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                cur = os.read(fd, frame_bytes)
            except OSError as e:
                log('fb 读取失败: %s' % e)
                break
            if len(cur) < frame_bytes:
                time.sleep(0.5)
                continue

            if prev is None:
                rows = [cur[y * stride:y * stride + w * 2] for y in range(h)]
                payload = b'\x01' + struct.pack('>HH', w, h) + b''.join(rows)
                ws_send(sock, 0x2, payload)
                prev = bytearray(cur)
                sent_bytes += len(payload)
                sent_frames += 1
            elif cur == prev:
                # 整帧没变 —— 绝大多数时候都是这种。
                # bytes/bytearray 比较走 C 层 memcmp，整屏 1.2MB 只要 9ms；
                # 而下面 160 个块逐行比较要 183ms（板上实测）。
                # 这个快速路径把每轮开销从 405ms 压到 231ms。
                pass
            else:
                changed = 0
                for ty in range(0, h, TILE):
                    th = min(TILE, h - ty)
                    for tx in range(0, w, TILE):
                        tw = min(TILE, w - tx)
                        row_bytes = tw * 2
                        same = True
                        for y in range(ty, ty + th):
                            o = y * stride + tx * 2
                            if cur[o:o + row_bytes] != prev[o:o + row_bytes]:
                                same = False
                                break
                        if same:
                            continue
                        rows = []
                        for y in range(ty, ty + th):
                            o = y * stride + tx * 2
                            rows.append(cur[o:o + row_bytes])
                            prev[o:o + row_bytes] = cur[o:o + row_bytes]
                        payload = (b'\x02' + struct.pack('>HHHH', tx, ty, tw, th) +
                                   b''.join(rows))
                        ws_send(sock, 0x2, payload)
                        sent_bytes += len(payload)
                        changed += 1
                if changed:
                    sent_frames += 1

            # 客户端消息：close 断开；{"t":"full"} 请求重发整屏（自愈用）
            try:
                r, _, _ = select.select([sock], [], [], 0)
                if r:
                    op, data = ws_recv(sock)
                    if op == 0x8:
                        break
                    if op == 0x1:
                        try:
                            if json.loads(data.decode('utf-8', 'replace')).get('t') == 'full':
                                prev = None
                        except Exception:
                            pass
            except Exception:
                break

            dt = time.time() - t0
            if dt < interval:
                time.sleep(interval - dt)
    except (EOFError, OSError):
        pass
    except Exception:
        log('fb 异常: ' + traceback.format_exc().replace('\n', ' | '))
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
        log('fb: 结束，推送 %d 帧 / %d KB' % (sent_frames, sent_bytes // 1024))


# --------------------------------------------------------------------------
# 触摸注入
#
# 板子的内核没编 uinput（CONFIG_INPUT_UINPUT is not set），没有 /dev/uinput，
# 也不支持往 evdev 里写事件，所以"标准"的用户态注入走不通。
#
# 这里换了个落点：板上 tslib 的输入模块 input.so 被换成了带注入通道的版本
# （deploy/touch/），它会 connect 到本进程的这个 unix socket，把收到的
# input_event 和物理触摸事件一起喂进 tslib 的解码流程。
#
# 用 SOCK_SEQPACKET、一次 write 一条事件：保证对端每次 read 拿到的是
# 完整的一个 struct input_event（16 字节），不会切一半。
# --------------------------------------------------------------------------
EV_SYN = 0
EV_KEY = 1
EV_ABS = 3
BTN_TOUCH = 0x14a
ABS_X = 0x00
ABS_Y = 0x01
ABS_PRESSURE = 0x18
ABS_MT_SLOT = 0x2f
ABS_MT_TOUCH_MAJOR = 0x30
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39
SYN_REPORT = 0

TOUCH_SOCK = '/tmp/board-touch.sock'


def pack_ev(etype, code, value):
    """32 位 ARM 的 struct input_event：timeval(2×int32) + u16 + u16 + s32 = 16 字节"""
    return struct.pack('<iiHHi', int(time.time()), 0, etype, code, value)


class TouchInject(object):
    def __init__(self):
        self.srv = None
        self.conns = []          # 可能不止一个消费者（主进程/子进程都会 preload）
        self.lock = threading.Lock()
        self.seq = 100
        self.pressed = False
        self.sent = 0

    def start(self):
        try:
            os.unlink(TOUCH_SOCK)
        except OSError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        s.bind(TOUCH_SOCK)
        os.chmod(TOUCH_SOCK, 0o666)
        s.listen(1)
        self.srv = s
        t = threading.Thread(target=self._accept_loop)
        t.daemon = True
        t.start()
        log('touch: 注入通道监听 %s' % TOUCH_SOCK)

    def _accept_loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                time.sleep(0.5)
                continue
            with self.lock:
                self.conns.append(c)
            log('touch: 输入模块已接入（当前 %d 个）' % len(self.conns))

    def _send(self, data):
        """向所有连接的消费者广播一条事件。

        不能只保留最后一个：同一进程的主线程/QML 线程、以及 preload 进去的
        子进程都会各连一条，关掉旧的会让对方以为通道断了、反复重连互踩。
        """
        ok = False
        dead = []
        with self.lock:
            for c in self.conns:
                try:
                    c.send(data)
                    ok = True
                except OSError:
                    dead.append(c)
            for c in dead:
                try:
                    c.close()
                except Exception:
                    pass
                if c in self.conns:
                    self.conns.remove(c)
        if dead:
            log('touch: %d 个消费者断开，剩 %d 个' % (len(dead), len(self.conns)))
        if ok:
            self.sent += 1
        return ok

    def _xy(self, x, y):
        x = max(0, min(FB_W - 1, int(x)))
        y = max(0, min(FB_H - 1, int(y)))
        return x, y

    def down(self, x, y):
        """按下：同时发 MT 协议 B 和单点事件。

        这块屏的 evdev 能力里只有 ABS_MT_*，没有 ABS_X/Y；
        而不同的消费者（Qt 内置 evdev 触摸 / tslib）认的协议不一样，
        两套都发，谁认哪套就用哪套，互不干扰。
        """
        x, y = self._xy(x, y)
        self.seq = (self.seq + 1) & 0xff or 100
        evs = [
            # 只发单点：实测 MT 事件和单点混发反而会让处理器只吃前两个就停
            (EV_KEY, BTN_TOUCH, 1),
            (EV_ABS, ABS_PRESSURE, 60),
            (EV_ABS, ABS_X, x),
            (EV_ABS, ABS_Y, y),
            (EV_SYN, SYN_REPORT, 0),
        ]
        for e in evs:
            self._send(pack_ev(*e))
        self.pressed = True
        return True

    def move(self, x, y):
        if not self.pressed:
            return False
        x, y = self._xy(x, y)
        for e in ((EV_ABS, ABS_X, x),
                  (EV_ABS, ABS_Y, y),
                  (EV_SYN, SYN_REPORT, 0)):
            self._send(pack_ev(*e))
        return True

    def up(self):
        if not self.pressed:
            return False
        for e in ((EV_KEY, BTN_TOUCH, 0),
                  (EV_ABS, ABS_PRESSURE, 0),
                  (EV_SYN, SYN_REPORT, 0)):
            self._send(pack_ev(*e))
        self.pressed = False
        return True

    def tap(self, x, y, hold=0.06):
        self.down(x, y)
        time.sleep(max(0.02, min(0.5, hold)))
        self.up()
        return True


INJECT = TouchInject()


# --------------------------------------------------------------------------
# GUI 遥控：直接拉起 /opt/ui/src/apps 里的程序，或者重启启动器
#
# 为什么走"指令级"而不是模拟触摸：这块板内核没编 uinput，套接字注入那条路
# （DEPLOY 第 18 节）时灵时不灵。直接 exec 应用是能用的下限。
#
# 实测出来的限制：从 agent 拉起的 app 不在 systemui 的进程树里，
# 它退出后 systemui 不会重绘，画面会残留 app 的最后一帧。
# 所以"回到桌面"只能是「杀掉 app + 重启 systemui」，约 4 秒。
# --------------------------------------------------------------------------
GUI_DIR = '/opt/ui/src'
GUI_BIN = '/opt/ui/systemui'
GUI_CFGS = ('/opt/ui/src/ATK-IMX6U/apk1.cfg',
            '/opt/ui/src/ATK-IMX6U/apk2.cfg')


def list_apps():
    """解析启动器 cfg，返回 [{'id': 'camera', 'name': '相机'}]

    cfg 每行三个字段：appicons/camera.png 相机 camera
    """
    apps = []
    seen = set()
    for path in GUI_CFGS:
        try:
            # 必须显式 utf-8：板上默认 locale 是 ASCII，cfg 里的中文会炸
            f = open(path, 'r', encoding='utf-8', errors='replace')
        except IOError:
            continue
        try:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[-1] not in seen:
                    seen.add(parts[-1])
                    apps.append({'id': parts[-1], 'name': parts[-2]})
        finally:
            f.close()
    return apps


def find_pids(exe):
    """按 cmdline[0] 精确匹配。

    不能用 ps：BusyBox 的 ps 输出会被截断，`grep board-agent` 这种匹配会漏。
    """
    out = []
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        try:
            f = open('/proc/%s/cmdline' % d, 'rb')
            try:
                cmd = f.read().split(b'\0')
            finally:
                f.close()
            if cmd and cmd[0].decode('utf-8', 'replace') == exe:
                out.append(int(d))
        except Exception:
            pass
    return out


def _terminate(pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def launch_app(name):
    ids = [a['id'] for a in list_apps()]
    if name not in ids:
        return False, 'unknown app'
    subprocess.Popen(['/bin/sh', '-c', '. /etc/profile; exec ./apps/' + name],
                     cwd=GUI_DIR,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     start_new_session=True)   # 脱离 agent 的会话，断连接不会带走它
    log('ctl: 启动应用 %s' % name)
    return True, name


def back_to_desk():
    """杀掉在跑的应用，再重启 systemui —— 实测这是唯一能恢复桌面的办法。"""
    killed = []
    for a in list_apps():
        pids = find_pids('./apps/' + a['id']) + find_pids(GUI_DIR + '/apps/' + a['id'])
        if pids:
            killed.append(a['id'])
        _terminate(pids)
    _terminate(find_pids(GUI_BIN))
    time.sleep(1.0)
    subprocess.Popen(['/bin/sh', '-c', '. /etc/profile; exec ' + GUI_BIN],
                     cwd=GUI_DIR,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     start_new_session=True)
    log('ctl: 回到桌面（结束 %s，重启 systemui）' % (','.join(killed) or '无应用'))
    return True



def serve_input(handler):
    """接收浏览器发来的指针事件，转成触摸注入。"""
    sock = handler.connection
    log('input: 控制通道已连接')
    try:
        last_ping = time.time()
        while True:
            r, _, _ = select.select([sock], [], [], 5.0)
            if not r:
                if time.time() - last_ping > 15:
                    ws_send(sock, 0x9, b'')
                    last_ping = time.time()
                continue
            op, data = ws_recv(sock)
            if op == 0x8:
                break
            if op == 0x9:
                ws_send(sock, 0xA, data)
                continue
            if op != 0x1:
                continue
            try:
                msg = json.loads(data.decode('utf-8', 'replace'))
            except Exception:
                continue
            t = msg.get('t')
            if t == 'down':
                INJECT.down(msg.get('x', 0), msg.get('y', 0))
            elif t == 'move':
                INJECT.move(msg.get('x', 0), msg.get('y', 0))
            elif t == 'up':
                INJECT.up()
            elif t == 'tap':
                INJECT.tap(msg.get('x', 0), msg.get('y', 0), msg.get('h', 0.06))
    except (EOFError, OSError):
        pass
    except Exception:
        log('input 异常: ' + traceback.format_exc().replace('\n', ' | '))
    finally:
        INJECT.up()
        log('input: 控制通道断开')


# --------------------------------------------------------------------------
# HTTP / WS 入口
# --------------------------------------------------------------------------
class Handler(socketserver.StreamRequestHandler):
    """自己实现最小 HTTP，避开 BaseHTTPRequestHandler 对长连接的假设"""

    def handle(self):
        try:
            self.raw = self.request.makefile('rb', 0)
            line = self.raw.readline(4096)
            if not line:
                return
            parts = line.decode('latin-1').split()
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]
            headers = {}
            while True:
                h = self.raw.readline(4096)
                if not h or h in (b'\r\n', b'\n'):
                    break
                k, _, v = h.decode('latin-1').partition(':')
                headers[k.strip().lower()] = v.strip()

            path, _, query = target.partition('?')
            if path.startswith('/agent/'):
                path = path[len('/agent'):]
            path = path.rstrip('/') or '/'
            qs = {}
            for kv in query.split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    # 必须解码：前端对 action 做了 encodeURIComponent，
                    # 不解的话 'app:camera' 会变成 'app%3Acamera' 匹配不上
                    qs[unquote(k)] = unquote(v)

            with REQ_LOCK:
                REQUESTS[0] += 1

            if path == '/api/status':
                self.send_json(collect_status())
                return
            if path == '/api/health':
                self.send_json({'ok': True, 'ver': VERSION})
                return
            if path == '/ws/shell':
                if not self.auth(headers):
                    return
                if not self.upgrade(headers):
                    return
                rows = int(qs.get('r', 30) or 30)
                cols = int(qs.get('c', 100) or 100)
                serve_shell(self, min(rows, 200), min(cols, 400))
                return
            if path == '/ws/fb':
                if not self.auth(headers):
                    return
                if not self.upgrade(headers):
                    return
                try:
                    iv = float(qs.get('i', FB_DEFAULT_INTERVAL))
                except Exception:
                    iv = FB_DEFAULT_INTERVAL
                serve_fb(self, max(0.3, min(5.0, iv)))
                return
            if path == '/api/apps':
                tok = headers.get('x-board-token', '')
                if not hmac.compare_digest(tok, TOKEN):
                    time.sleep(0.3)
                    self.send_text(401, 'invalid token\n')
                    return
                self.send_json({'apps': list_apps()})
                return
            if path == '/api/ctl':
                # 指令级遥控：a=app:<名字> 启动应用；a=desk 回桌面
                tok = headers.get('x-board-token', '')
                if not hmac.compare_digest(tok, TOKEN):
                    time.sleep(0.3)
                    self.send_text(401, 'invalid token\n')
                    return
                act = qs.get('a', '')
                if act.startswith('app:'):
                    ok, msg = launch_app(act[4:])
                    self.send_json({'ok': ok, 'msg': msg})
                    return
                if act == 'desk':
                    self.send_json({'ok': back_to_desk()})
                    return
                self.send_text(400, 'bad action\n')
                return
            if path == '/api/tap':
                # 方便用 curl 验证注入；token 走请求头，不进 URL（避免被日志记下）
                tok = headers.get('x-board-token', '')
                if not hmac.compare_digest(tok, TOKEN):
                    time.sleep(0.3)
                    self.send_text(401, 'invalid token\n')
                    return
                try:
                    x = float(qs.get('x', '0'))
                    y = float(qs.get('y', '0'))
                    h = float(qs.get('h', '0.06'))
                except Exception:
                    self.send_text(400, 'bad x/y\n')
                    return
                INJECT.tap(x, y, h)
                self.send_json({'ok': True, 'connected': len(INJECT.conns) > 0,
                                'sent': INJECT.sent})
                return
            if path == '/ws/input':
                if not self.auth(headers):
                    return
                if not self.upgrade(headers):
                    return
                serve_input(self)
                return
            if path in ('/', '/index.html'):
                self.send_text(200, 'board-agent %s\nroutes: /agent/api/status, '
                                    '/agent/ws/shell, /agent/ws/fb, /agent/api/apps, /agent/api/ctl\n' % VERSION)
                return
            self.send_text(404, 'not found\n')
        except Exception:
            log('http 异常: ' + traceback.format_exc().replace('\n', ' | '))

    # ---- helpers ----
    def auth(self, headers):
        plist = [p.strip() for p in headers.get('sec-websocket-protocol', '').split(',') if p.strip()]
        tok = None
        for p in plist:
            if p != SUB_PROTO:
                tok = p
        ok = bool(tok) and hmac.compare_digest(tok, TOKEN)
        if not ok:
            time.sleep(0.6)   # 拖慢暴力猜测
            self.send_text(401, 'invalid token\n')
            log('auth 失败 %s 收到token长度=%d 应为=%d'
                % (self.client_address[0], len(tok or ''), len(TOKEN)))
        return ok

    def upgrade(self, headers):
        key = headers.get('sec-websocket-key')
        if not key:
            self.send_text(400, 'no websocket key\n')
            return False
        resp = ('HTTP/1.1 101 Switching Protocols\r\n'
                'Upgrade: websocket\r\n'
                'Connection: Upgrade\r\n'
                'Sec-WebSocket-Accept: %s\r\n'
                'Sec-WebSocket-Protocol: %s\r\n'
                '\r\n' % (ws_accept(key), SUB_PROTO))
        self.request.sendall(resp.encode())
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        return True

    def send_json(self, obj):
        body = json.dumps(obj, separators=(',', ':')).encode()
        hdr = ('HTTP/1.1 200 OK\r\n'
               'Content-Type: application/json; charset=utf-8\r\n'
               'Cache-Control: no-store\r\n'
               'Content-Length: %d\r\n'
               'Connection: close\r\n\r\n' % len(body))
        self.request.sendall(hdr.encode() + body)

    def send_text(self, code, text, ctype='text/plain; charset=utf-8'):
        body = text.encode()
        hdr = ('HTTP/1.1 %d %s\r\n'
               'Content-Type: %s\r\n'
               'Content-Length: %d\r\n'
               'Connection: close\r\n\r\n'
               % (code, 'OK' if code == 200 else 'ERR', ctype, len(body)))
        self.request.sendall(hdr.encode() + body)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 16


TOKEN = ''


def main():
    global TOKEN, VERBOSE, PORT, HOST
    args = sys.argv[1:]
    if '-v' in args:
        VERBOSE = True
    if '-p' in args:
        PORT = int(args[args.index('-p') + 1])
    if '-b' in args:
        HOST = args[args.index('-b') + 1]

    fb_geometry()
    INJECT.start()
    TOKEN = load_token()
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)   # 自动回收 shell 子进程
    except Exception:
        pass

    srv = Server((HOST, PORT), Handler)
    log('board-agent %s 启动 %s:%d token=%d字符 fb=%dx%d@%dbpp stride=%d'
        % (VERSION, HOST, PORT, len(TOKEN), FB_W, FB_H, FB_BPP, FB_STRIDE))
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log('收到中断，退出')
        srv.shutdown()


if __name__ == '__main__':
    main()
