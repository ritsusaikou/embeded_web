---
title: TFTP 与 NFS 网络环境搭建
order: 3
week: 第三次
date: 2026-10-08
summary: 用 TFTP 让 u-boot 从网络取内核与设备树，用 NFS 直接挂载根文件系统，从此改代码不再需要反复插拔 TF 卡。
tags: [TFTP, NFS, u-boot, 网络调试]
duration: 4 学时
hardware: [i.MX6ULL 开发板, 网线, 路由器]
---

## 实验目的

1. 掌握 TFTP 与 NFS 两种网络服务的工作原理与配置方法。
2. 让 u-boot 通过 TFTP 加载 `zImage` 与 `.dtb`，实现内核免烧写更新。
3. 让内核通过 NFS 挂载根文件系统，实现应用层「保存即生效」的开发闭环。

## 实验环境

| 角色 | IP | 说明 |
| --- | --- | --- |
| 宿主机（Ubuntu） | 192.168.1.100 | 兼作 TFTP / NFS 服务器 |
| 开发板 | 192.168.1.50 | 与宿主机同网段，桥接模式 |

服务：`tftpd-hpa`（UDP 69）、`nfs-kernel-server`（TCP 2049）。

## 原理与要点

- **TFTP** 是基于 UDP 69 的极简文件传输协议，实现短、无认证，正好适合 u-boot 阶段——此时还没有完整网络栈。它解决「**内核镜像和设备树怎么进板子**」。
- **NFS** 是网络文件系统，内核把远端目录当本地目录挂载。它解决「**根文件系统怎么进板子**」，改一个文件不用重新打包镜像。

两者合起来，整个开发流程从「改代码 → 编译 → 烧卡 → 上电」变成「改代码 → 编译 → 敲一行命令」。

## 实验步骤

### 1. 宿主机配置 TFTP

```bash
sudo apt install tftp-hpa tftpd-hpa
sudo vi /etc/default/tftpd-hpa
# TFTP_DIRECTORY="/home/user/tftpboot"
# TFTP_OPTIONS="-l -c -s"
sudo chmod 777 /home/user/tftpboot
sudo systemctl restart tftpd-hpa
```

### 2. 宿主机配置 NFS

```bash
sudo apt install nfs-kernel-server
# /etc/exports 追加：
# /home/user/nfs/rootfs *(rw,sync,no_subtree_check,no_root_squash)
sudo exportfs -arv
sudo systemctl restart nfs-kernel-server
```

### 3. u-boot 侧配置

```text
setenv ipaddr 192.168.1.50
setenv serverip 192.168.1.100
setenv gatewayip 192.168.1.1
setenv bootargs 'console=ttymxc0,115200 root=/dev/nfs rw nfsroot=192.168.1.100:/home/user/nfs/rootfs,proto=tcp ip=192.168.1.50:192.168.1.100:192.168.1.1:255.255.255.0::eth0:off'
saveenv
ping 192.168.1.100
```

### 4. 网络启动内核

```text
tftp 80800000 zImage
tftp 83000000 imx6ull-alientek-emmc.dtb
bootz 80800000 - 83000000
```

## 实验结果

`ping` 通服务器后，TFTP 每次传输显示进度与速率（约 1MB/s，百兆网口受 CPU 限制）；内核启动日志末尾出现：

```text
VFS: Mounted root (nfs filesystem) on device 0:15.
```

说明根文件系统已通过网络挂载。此时在宿主机 `rootfs` 目录下新增一个脚本，板子上刷新即见，验证了「保存即生效」。

## 问题与解决

**虚拟机 NAT 模式完全不通。** NAT 下虚拟机在另一层子网，板子 ping 不到。改成**桥接模式**并手动指定静态 IP，问题消失。这是本实验耗时最久的坑，属于网络拓扑问题而不是服务配置问题。

**NFS 挂载超时。** 内核日志反复 `portmapper: no response`。原因是 Ubuntu 默认走 NFSv4，而 4.1.15 老内核的客户端与新版服务端协商失败。加 `proto=tcp` 并显式指定 `nfsvers=3` 后正常。

**TFTP 报 `Permission denied`。** 上传/下载目录权限不足。`-c` 参数允许创建文件，目录本身还需 `chmod 777`。

**bootargs 写错导致根文件系统找不到。** 注意 `nfsroot=` 里的 IP 是**服务器**的，而 `ip=` 的顺序是 `本机:服务器:网关:掩码`，写反会卡在 `Waiting for root device`。

## 小结

这次实验把「开发迭代成本」降了一个数量级。更重要的是理解了嵌入式开发的分层调试手段：**u-boot 阶段用 TFTP 换内核，内核阶段用 NFS 换 rootfs，应用阶段用 scp 换程序**。后面写驱动、调 Qt 应用，全靠这套环境在撑着。
