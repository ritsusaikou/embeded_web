/* ============================================================
 * touch-inject.c —— 给 Qt 应用加一条"触摸注入"侧门
 *
 * 背景：
 *   板子内核没编 uinput（CONFIG_INPUT_UINPUT is not set），没法造虚拟输入
 *   设备；evdev 也不支持用户态写事件。所以只能挂在"应用读设备"这一步上。
 *
 *   实测（shim-probe）：systemui 的输入来自 libqlinuxfb 内置的 evdev 触摸，
 *   主线程 poll() 的集合里有 fd 9 → /dev/input/event1。它永远是
 *   "poll 说可读 → 再 read 16 字节 struct input_event" 这个节奏。
 *
 * 做法：
 *   1) 拦截 poll/ppoll：如果集合里含触摸设备，就把我们的 unix socket 也加进去等；
 *      socket 有数据时，把对应的设备 fd 标成可读（让应用去 read）。
 *   2) 拦截 read：如果是触摸设备 fd，且 socket 里有事件，就返回注入的事件；
 *      否则照常读设备。
 *
 *   物理触摸完全不受影响：没有注入事件时，行为和原版一模一样；
 *   agent 不在时（socket 连不上），这个 .so 等于不存在。
 *
 * 编译：见 build-shim.sh（只依赖 GLIBC_2.4，不用 dlsym）
 * 用法：LD_PRELOAD=/usr/local/lib/touch-inject.so /opt/ui/systemui
 * ============================================================ */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <stdarg.h>

#define TOUCH_SOCK_DEFAULT "/tmp/board-touch.sock"

/* 设 BOARD_TOUCH_DEBUG=1 时把关键动作写到 /tmp/ti.log */
static int dbg_on = -1;
static void tlog(const char *fmt, ...)
{
	char buf[256];
	int n, fd;
	va_list ap;

	if (dbg_on < 0)
		dbg_on = getenv("BOARD_TOUCH_DEBUG") ? 1 : 0;
	if (!dbg_on)
		return;
	fd = (int)syscall(SYS_open, "/tmp/ti.log", O_WRONLY | O_CREAT | O_APPEND, 0622);
	if (fd < 0)
		return;
	va_start(ap, fmt);
	n = vsnprintf(buf, sizeof(buf), fmt, ap);
	va_end(ap);
	syscall(SYS_write, fd, buf, n);
	syscall(SYS_close, fd);
}
#define FDCACHE 256
#define MAXPOLL 64
#define EV_SIZE 16        /* sizeof(struct input_event) on 32-bit ARM */

static signed char touch_cache[FDCACHE];   /* 0 未判定 / 1 否 / 2 是 */
static int inject_fd = -1;
static time_t last_try;
static volatile int connecting;
/* 本轮 poll 里等到的触摸 fd —— 事件只投给它，避免被另一个打开同一设备的线程读走 */
static volatile int target_fd = -1;
static unsigned int tick;   /* 0 空闲 / 1 正在连（多线程下只允许一个连） */

static int is_touch_fd(int fd)
{
	char path[64], link[128];
	long n;

	if (fd < 0 || fd >= FDCACHE)
		return 0;

	/*
	 * 不能永久缓存"不是触摸设备"：fd 会被复用（比如启动时 9 号是 eventfd，
	 * 关掉以后 9 号又变成 /dev/input/event1）。所以负结果每 64 次重查一遍。
	 */
	if (touch_cache[fd]) {
		if (touch_cache[fd] == 2)
			return 1;
		if (++tick & 0x3f)
			return 0;
	}

	snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
	n = syscall(SYS_readlink, path, link, sizeof(link) - 1);
	if (n <= 0) {
		touch_cache[fd] = 1;
		return 0;
	}
	link[n] = 0;
	/* 只认触摸屏那一个；event0/event2 是电源键和 GPIO 键 */
	if (strstr(link, "/dev/input/event1")) {
		touch_cache[fd] = 2;
		tlog("touch fd=%d\n", fd);
		return 1;
	}
	touch_cache[fd] = 1;
	return 0;
}

static void try_connect(void)
{
	const char *path = getenv("BOARD_TOUCH_SOCK");
	struct sockaddr_un sa;
	time_t now = time(NULL);
	long fd;

	if (inject_fd >= 0)
		return;
	if (now - last_try < 2)
		return;
	/*
	 * 主线程和 QML 线程都会 poll，两边同时调到这里会各自 connect 一次，
	 * agent 端只保留最后一个并关掉前一个，结果就是我们手里的 fd 被对端
	 * 关掉、注入全部丢进黑洞。用一条原子锁保证只连一次。
	 */
	if (__sync_lock_test_and_set(&connecting, 1))
		return;
	last_try = now;

	if (!path || !*path)
		path = TOUCH_SOCK_DEFAULT;

	fd = syscall(SYS_socket, AF_UNIX, SOCK_SEQPACKET, 0);
	if (fd >= 0) {
		memset(&sa, 0, sizeof(sa));
		sa.sun_family = AF_UNIX;
		strncpy(sa.sun_path, path, sizeof(sa.sun_path) - 1);

		if (syscall(SYS_connect, fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) {
			syscall(SYS_close, fd);
		} else {
					inject_fd = (int)fd;
			tlog("connected fd=%d\n", inject_fd);
		}
	}
	__sync_lock_release(&connecting);
}

/* 从注入通道取一个事件（不阻塞）。返回 >0 表示拿到了。 */
static long take_event(void *buf, size_t len)
{
	long n;

	if (inject_fd < 0)
		return -1;
	n = syscall(SYS_recvfrom, inject_fd, buf, len, MSG_DONTWAIT, NULL, NULL);
	if (n == 0) {                       /* 对端关闭 */
		syscall(SYS_close, inject_fd);
		inject_fd = -1;
		return -1;
	}
	if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
		syscall(SYS_close, inject_fd);
		inject_fd = -1;
		return -1;
	}
	return n;
}

ssize_t read(int fd, void *buf, size_t count)
{
	if (is_touch_fd(fd) && inject_fd >= 0) {
		long n = take_event(buf, count);
		if (n > 0) {
			tlog("inject read fd=%d n=%ld\n", fd, n);
			return (ssize_t)n;
		}
	}
	return (ssize_t)syscall(SYS_read, fd, buf, count);
}

static int do_poll(struct pollfd *fds, nfds_t nfds, int timeout_ms,
		   const struct timespec *tmo, const sigset_t *sigmask, int use_ppoll)
{
	struct pollfd tmp[MAXPOLL];
	nfds_t i, m = 0;
	int touch_idx = -1;
	long r;

	if (nfds == 0 || nfds > MAXPOLL - 1)
		goto plain;

	for (i = 0; i < nfds; i++) {
		tmp[m] = fds[i];
		if (is_touch_fd(fds[i].fd))
			touch_idx = (int)m;
		m++;
	}

	if (touch_idx >= 0) {
		try_connect();
		if (inject_fd >= 0) {
			tmp[m].fd = inject_fd;
			tmp[m].events = POLLIN;
			tmp[m].revents = 0;
			m++;
		}
	}

	if (use_ppoll)
		r = syscall(SYS_ppoll, tmp, m, tmo, sigmask, sizeof(sigset_t));
	else
		r = syscall(SYS_poll, tmp, m, timeout_ms);
	if (r < 0)
		return (int)r;

	/* 把结果拷回调用者的数组 */
	for (i = 0; i < nfds; i++)
		fds[i].revents = tmp[i].revents;

	/* socket 有事件 → 让触摸设备"看起来可读"，应用才会去 read */
	if (touch_idx >= 0 && inject_fd >= 0 && m > nfds &&
	    (tmp[nfds].revents & POLLIN)) {
		fds[touch_idx].revents |= POLLIN;
		if (r == 0)
			r = 1;
	}
	return (int)r;

plain:
	if (use_ppoll)
		return (int)syscall(SYS_ppoll, fds, nfds, tmo, sigmask, sizeof(sigset_t));
	return (int)syscall(SYS_poll, fds, nfds, timeout_ms);
}

int poll(struct pollfd *fds, nfds_t nfds, int timeout)
{
	return do_poll(fds, nfds, timeout, NULL, NULL, 0);
}

int ppoll(struct pollfd *fds, nfds_t nfds, const struct timespec *tmo,
	  const sigset_t *sigmask)
{
	return do_poll(fds, nfds, 0, tmo, sigmask, 1);
}
