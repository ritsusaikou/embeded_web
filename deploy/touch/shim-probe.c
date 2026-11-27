/* ============================================================
 * shim-probe.c —— 只记日志的探针（dry-run）
 *
 * 目的：搞清楚 Qt 到底用哪条路径读触摸设备
 *   1) read() 是否作用在 /dev/input/event*
 *   2) poll() 是否把这些 fd 加进等待集合
 *
 * 实现要点：
 *   - 不用 dlsym(RTLD_NEXT)。glibc 2.34 才把 dlsym 并进 libc，
 *     板子是 2.23，编译出来会要求 dlsym@GLIBC_2.34 → 直接加载失败。
 *     这里用 syscall() 走原始系统调用，只依赖 GLIBC_2.4。
 *
 * 用法：LD_PRELOAD=/tmp/shim.so /opt/ui/systemui
 * 日志：/tmp/shim.log
 * ============================================================ */
#define _GNU_SOURCE
#include <fcntl.h>
#include <poll.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <signal.h>
#include <sys/select.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

static void slog(const char *fmt, ...)
{
	int fd = open("/tmp/shim.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
	char buf[256];
	int n;
	va_list ap;

	if (fd < 0)
		return;
	n = snprintf(buf, sizeof(buf), "[%ld] ", (long)time(NULL));
	va_start(ap, fmt);
	n += vsnprintf(buf + n, sizeof(buf) - n, fmt, ap);
	va_end(ap);
	buf[n++] = '\n';
	syscall(SYS_write, fd, buf, n);
	syscall(SYS_close, fd);
}

#define FDCACHE 512
static signed char fd_is_input[FDCACHE];

static int is_input_fd(int fd)
{
	char path[64], link[128];
	long n;

	if (fd < 0 || fd >= FDCACHE)
		return 0;
	if (fd_is_input[fd] >= 0)
		return fd_is_input[fd];

	snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
	n = syscall(SYS_readlink, path, link, sizeof(link) - 1);
	if (n <= 0) {
		fd_is_input[fd] = 0;
		return 0;
	}
	link[n] = 0;
	fd_is_input[fd] = (strstr(link, "/dev/input/event") != NULL) ? 1 : 0;
	if (fd_is_input[fd])
		slog("fd %d -> %s", fd, link);
	return fd_is_input[fd];
}

static signed char seen[FDCACHE];

static void note_fd(int fd)
{
	char path[64], link[128];
	long n;

	if (fd < 0 || fd >= FDCACHE || seen[fd])
		return;
	seen[fd] = 1;
	snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
	n = syscall(SYS_readlink, path, link, sizeof(link) - 1);
	if (n > 0) {
		link[n] = 0;
		slog("poll 里出现 fd=%d -> %s", fd, link);
	} else {
		slog("poll 里出现 fd=%d -> (readlink失败 %ld)", fd, n);
	}
}

ssize_t read(int fd, void *buf, size_t count)
{
	long n = syscall(SYS_read, fd, buf, count);
	if (is_input_fd(fd))
		slog("read(fd=%d, %zu) -> %ld", fd, count, n);
	return (ssize_t)n;
}

int poll(struct pollfd *fds, nfds_t nfds, int timeout)
{
	static long last_log;
	long now = (long)time(NULL);
	int n = (int)syscall(SYS_poll, fds, nfds, timeout);
	nfds_t i;

	for (i = 0; i < nfds; i++)
		note_fd(fds[i].fd);

	for (i = 0; i < nfds; i++) {
		if (is_input_fd(fds[i].fd)) {
			if (now - last_log >= 5) {
				slog("poll 正在等待输入设备 fd=%d (nfds=%d timeout=%d revents=%d)",
				     fds[i].fd, (int)nfds, timeout, fds[i].revents);
				last_log = now;
			}
			break;
		}
	}
	return n;
}

int ppoll(struct pollfd *fds, nfds_t nfds, const struct timespec *tmo,
	  const sigset_t *sigmask)
{
	static long last_log;
	long now = (long)time(NULL);
	nfds_t i;
	int n = (int)syscall(SYS_ppoll, fds, nfds, tmo, sigmask, sizeof(sigset_t));

	for (i = 0; i < nfds; i++)
		note_fd(fds[i].fd);

	for (i = 0; i < nfds; i++) {
		if (is_input_fd(fds[i].fd)) {
			if (now - last_log >= 5) {
				slog("ppoll 正在等待输入设备 fd=%d (nfds=%d revents=%d)",
				     fds[i].fd, (int)nfds, fds[i].revents);
				last_log = now;
			}
			break;
		}
	}
	return n;
}
