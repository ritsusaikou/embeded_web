---
title: 嵌入式 Linux 开发环境搭建与简单应用程序编写
order: 1
week: 第一次
date: 2026-09-17
summary: 在 Ubuntu 上装好交叉工具链与串口终端，完成第一个 ARM 程序的编译、传输、运行，把「宿主机 / 目标机」这条链路彻底打通。
tags: [环境搭建, 交叉编译, 串口调试]
duration: 4 学时
hardware: [i.MX6ULL 开发板, USB 转串口模块, 网线]
---

## 实验目的

1. 建立「宿主机编译、目标机运行」的交叉开发模型，理解交叉编译与本地编译的区别。
2. 完成交叉编译工具链、串口终端、网络传输三件基础设施的搭建。
3. 独立编写、编译并在开发板上运行第一个 ARM Linux 应用程序。

## 实验环境

| 项目 | 说明 |
| --- | --- |
| 宿主机 | Ubuntu 20.04 LTS（VMware，4 核 / 4GB） |
| 目标机 | i.MX6ULL 开发板，出厂 Linux 4.1.15 |
| 工具链 | `gcc-linaro-4.9.4-2017.01-x86_64_arm-linux-gnueabihf` |
| 连接方式 | USB 转串口（115200 8N1）+ 网线（与宿主机同网段） |

## 原理与要点

宿主机是 x86_64，目标机是 ARMv7。两者的指令集、字长、ABI 都不同，**x86 上编译出来的程序在板子上无法执行**。交叉编译工具链（前缀 `arm-linux-gnueabihf-`）内部包含面向 ARM 的编译器、汇编器、链接器和一套 ARM 版 glibc，因此能在 x86 上产出 ARM 可执行文件。

判断一个文件是不是能跑在板子上，看两个地方：

- `file hello` 的 `ELF 32-bit LSB executable, ARM, EABI5`；
- `readelf -l hello` 的 interpreter 字段（动态链接时指向目标机 rootfs 里的 `ld-linux-armhf.so.3`）。

## 实验步骤

1. 解压工具链到 `/usr/local/arm/`，把 `bin` 目录加入 `PATH`，执行 `arm-linux-gnueabihf-gcc -v` 确认版本。
2. 编写 `hello.c`，编译并检查 ELF 属性。
3. 通过 `scp` 传到开发板（本实验先走网络传输，TFTP / NFS 在第三次实验专门搭建）。
4. 串口终端里 `chmod +x hello && ./hello`。

```bash
tar -xf gcc-linaro-4.9.4-2017.01-x86_64_arm-linux-gnueabihf.tar.xz -C /usr/local/arm
export PATH=$PATH:/usr/local/arm/gcc-linaro-4.9.4-2017.01-x86_64_arm-linux-gnueabihf/bin
arm-linux-gnueabihf-gcc -v
```

## 关键代码

```c
#include <stdio.h>
#include <sys/utsname.h>

int main(void)
{
    struct utsname u;
    uname(&u);
    printf("Hello from %s\n", u.nodename);
    printf("machine = %s, release = %s\n", u.machine, u.release);
    return 0;
}
```

```bash
arm-linux-gnueabihf-gcc hello.c -o hello -static
file hello
# hello: ELF 32-bit LSB executable, ARM, EABI5 version 1 (SYSV), statically linked
```

## 实验结果

串口终端输出：

```text
Hello from imx6ull-alpha
machine = armv7l, release = 4.1.15
```

`machine` 为 `armv7l`，确认程序确实运行在 ARM 内核上，不是被误传到宿主机执行。

## 问题与解决

**动态链接版本不匹配。** 第一次没用 `-static`，板子上直接报 `-sh: ./hello: not found`——这个提示具有极强误导性，它其实是**解释器缺失**（`ld-linux-armhf.so.3` 不存在或版本低于工具链要求的 GLIBC），不是文件不存在。用 `readelf -l hello` 查 interpreter 才定位到原因。解决办法两种：加 `-static` 静态链接，或把工具链对应的 lib 同步到板子并设 `LD_LIBRARY_PATH`。第一阶段统一用静态链接，排除 rootfs 差异。

**串口一直无输出。** 排查顺序：确认设备节点（Windows 下 COM 口、Linux 下 `/dev/ttyUSB0`）、波特率 115200、8 数据位无校验、关闭流控。当时是 minicom 开了硬件流控导致能连上但收不到字符。

**权限问题。** `/dev/ttyUSB0` 默认属于 `dialout` 组，普通用户需要 `sudo usermod -aG dialout $USER` 后重新登录。

## 小结

这次实验本身没有难度，但它决定了后面所有实验的效率。真正的收获是形成了固定判断链路：**程序跑不起来先看 `file` / `readelf`，再看 rootfs 的库，最后才怀疑代码。** 后面编译内核、写驱动时这套排查顺序一直沿用。
