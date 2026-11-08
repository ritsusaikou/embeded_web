---
title: 嵌入式 Linux 蜂鸣器驱动实验
order: 4
week: 第四次
date: 2026-10-15
summary: 第一次写完整的内核模块：设备树描述硬件、platform 总线匹配、gpiolib 申请引脚、字符设备暴露接口，用户态 echo 即可控制。
tags: [字符设备, platform, 设备树, gpiolib]
duration: 4 学时
hardware: [i.MX6ULL 开发板, 有源蜂鸣器]
---

## 实验目的

1. 掌握 Linux 驱动的分离思想：设备树管「硬件在哪里」，驱动管「怎么操作」。
2. 掌握 platform 总线匹配机制与 `probe` 函数执行时机。
3. 实现字符设备，用 `open / write / release` 把 GPIO 控制暴露到用户态。

## 实验环境

- 蜂鸣器接 **GPIO1_IO18**，有源蜂鸣器，**低电平鸣叫**。
- 内核源码：Linux 4.1.15（开发板厂商提供，已配置好 `.config`）。
- 编译：在宿主机用内核源码树编译模块，输出 `beep.ko`。

## 原理与要点

驱动的三段式：

1. **设备树**声明一个节点，`compatible = "alientek,beep"`；
2. **驱动**用 `of_match_table` 声明支持这个 compatible，内核启动时遍历设备树，匹配成功即调用 `probe`；
3. `probe` 里申请 GPIO、注册字符设备，之后用户态操作 `/dev/beep`。

关键点：**GPIO 编号不要写死**。`of_get_named_gpio()` 会从设备树读出「第几组第几号」并转成内核全局 GPIO 编号，硬件改板时只改设备树，驱动不动。

## 关键代码

### 设备树片段

```dts
beep {
    compatible = "alientek,beep";
    pinctrl-names = "default";
    pinctrl-0 = <&pinctrl_beep>;
    beep-gpio = <&gpio1 18 GPIO_ACTIVE_LOW>;
    status = "okay";
};
```

### 驱动核心

```c
static int beep_open(struct inode *inode, struct file *filp) { return 0; }

static ssize_t beep_write(struct file *filp, const char __user *buf,
                          size_t cnt, loff_t *off)
{
    unsigned char databuf[1];
    if (copy_from_user(databuf, buf, cnt)) return -EFAULT;
    /* 有源蜂鸣器低电平响 */
    gpio_set_value(beep.gpio, databuf[0] ? 0 : 1);
    return cnt;
}

static const struct file_operations beep_fops = {
    .owner = THIS_MODULE,
    .open  = beep_open,
    .write = beep_write,
};

static int beep_probe(struct platform_device *pdev)
{
    beep.gpio = of_get_named_gpio(pdev->dev.of_node, "beep-gpio", 0);
    if (!gpio_is_valid(beep.gpio)) return -EINVAL;

    gpio_request(beep.gpio, "beep");
    gpio_direction_output(beep.gpio, 1);   /* 初始化不响 */

    alloc_chrdev_region(&beep.devid, 0, 1, "beep");
    cdev_init(&beep.cdev, &beep_fops);
    cdev_add(&beep.cdev, beep.devid, 1);
    return 0;
}

static const struct of_device_id beep_of_match[] = {
    { .compatible = "alientek,beep" },
    { }
};
MODULE_DEVICE_TABLE(of, beep_of_match);

static struct platform_driver beep_driver = {
    .probe = beep_probe,
    .driver = { .name = "beep", .of_match_table = beep_of_match },
};
module_platform_driver(beep_driver);
```

### 测试

```bash
make -C /home/user/linux-imx M=$PWD modules
cp beep.ko /home/user/nfs/rootfs/lib/modules/4.1.15/
insmod beep.ko
dmesg | tail -5
mknod /dev/beep c 240 0      # 或由 class_create 自动创建
echo 1 > /dev/beep           # 响
echo 0 > /dev/beep           # 停
```

## 实验结果

`insmod` 后 dmesg 出现模块加载信息与 GPIO 申请成功日志；`echo 1 > /dev/beep` 蜂鸣器鸣叫，`echo 0` 停止，`cat /proc/devices` 能看到 `beep` 主设备号。

## 问题与解决

**模块加载了但 probe 没执行。** dmesg 无任何打印。两个原因都踩过：一是 `compatible` 拼写与设备树不一致（差一个字母也不会报错，只会静默不匹配）；二是改了 dts 却只 `make zImage` 没 `make dtbs`，板子上跑的还是旧设备树。判断方法：`ls /proc/device-tree/` 看节点是否存在。

**`gpio_request` 返回 `-EBUSY`。** 引脚被别的驱动先占了。检查 `/sys/kernel/debug/gpio` 看谁占着；本次是内核的 `led-gpio` 驱动用了同一组 GPIO，换引脚或删掉冲突节点即可。

**`/dev/beep` 不存在。** 手动 `mknod` 时主设备号必须与 `alloc_chrdev_region` 分配的一致，可以用 `cat /proc/devices` 核对；更规范的做法是 `class_create` + `device_create`，让 udev 自动建节点。

## 小结

从裸机直接写 `GPIO1_DR` 到内核驱动的 `gpio_set_value()`，中间隔着的不是复杂度，而是**抽象层次**。这次实验最大的收获是理解了设备树的意义：硬件差异被抽到 dts 里，同一份驱动源码可以适配不同底板，这才是嵌入式 Linux 能支撑海量板卡的原因。
