---
title: 裸机 LED 驱动实验
order: 2
week: 第二次
date: 2026-09-24
summary: 不依赖任何操作系统，直接配置 IOMUXC / CCM / GPIO 三组寄存器点亮 LED0，理解「时钟—复用—方向—电平」这条最小硬件控制链。
tags: [裸机, 寄存器, GPIO, IOMUX]
duration: 4 学时
hardware: [i.MX6ULL 开发板, TF 卡, LED0]
---

## 实验目的

1. 理解 i.MX6ULL 的启动流程与裸机程序的加载方式。
2. 掌握 IOMUXC（引脚复用）、CCM（时钟门控）、GPIO（方向与电平）三组寄存器的配置顺序。
3. 脱离操作系统，用最底层的方式让 LED0 闪烁。

## 实验环境

- 开发板：i.MX6ULL，LED0 接 **GPIO1_IO03**，**低电平点亮**。
- 工具链：`arm-linux-gnueabihf-gcc`，链接地址 `0x87800000`。
- 烧写：`imxdownload` 把 bin 写到 TF 卡（含 IVT / DCD 头，BootROM 靠它初始化 DDR）。

## 原理与要点

上电后芯片内部 BootROM 从 TF 卡固定偏移处读取 IVT，按 DCD 完成 DDR 初始化，然后把程序搬到 DDR 的链接地址开始执行。因此裸机 bin 的**链接地址必须是 DDR 中的合法地址**，且需要 imxdownload 添加头部。

点亮一个 GPIO 要做四件事，顺序不能反：

| 步骤 | 寄存器 | 地址 | 值 | 作用 |
| --- | --- | --- | --- | --- |
| 1. 开时钟 | `CCM_CCGR1` | `0x020C406C` | bit[27:26] = `0b11` | 给 GPIO1 供时钟，否则写寄存器无效 |
| 2. 引脚复用 | `IOMUXC_SW_MUX_CTL_PAD_GPIO1_IO03` | `0x020E0068` | `0x5` | 复用为 ALT5 = GPIO 功能 |
| 3. 电气属性 | `IOMUXC_SW_PAD_CTL_PAD_GPIO1_IO03` | `0x020E02F4` | `0x10B0` | 迟滞、上拉、驱动强度 |
| 4. 方向 + 电平 | `GPIO1_GDIR` / `GPIO1_DR` | `0x0209C004` / `0x0209C000` | bit3 = 1 / 0 | 输出方向，输出低电平 |

## 关键代码

```c
#define CCM_CCGR1       (*(volatile unsigned int *)0x020C406C)
#define SW_MUX_CTL_PAD  (*(volatile unsigned int *)0x020E0068)
#define SW_PAD_CTL_PAD  (*(volatile unsigned int *)0x020E02F4)
#define GPIO1_GDIR      (*(volatile unsigned int *)0x0209C004)
#define GPIO1_DR        (*(volatile unsigned int *)0x0209C000)

static void led_init(void)
{
    CCM_CCGR1 |= (3 << 26);          /* gpio1_clk_enable */
    SW_MUX_CTL_PAD = 0x5;            /* ALT5 -> GPIO1_IO03 */
    SW_PAD_CTL_PAD = 0x10B0;         /* 上拉 + 驱动能力 */
    GPIO1_GDIR |= (1 << 3);          /* bit3 输出 */
}

static void delay(volatile unsigned int n)
{
    while (n--) __asm__ volatile("nop");
}

int main(void)
{
    led_init();
    while (1) {
        GPIO1_DR &= ~(1 << 3);       /* 低电平 -> 点亮 */
        delay(0x800000);
        GPIO1_DR |= (1 << 3);        /* 高电平 -> 熄灭 */
        delay(0x800000);
    }
    return 0;
}
```

```bash
arm-linux-gnueabihf-gcc -g -c led.s -o led.o
arm-linux-gnueabihf-ld -Ttext 0x87800000 led.o -o led.elf
arm-linux-gnueabihf-objcopy -O binary -S -g led.elf led.bin
./imxdownload led.bin /dev/sdb
```

## 实验结果

插卡上电后 LED0 以约 0.5 秒周期稳定闪烁，串口无任何输出——说明程序完全运行在裸机环境，没有任何 OS 参与。

## 问题与解决

**LED 完全无反应。** 逐个寄存器回读发现 `CCM_CCGR1` 写入无效——地址写成了 `0x020C4068`（那是相邻的 `CCGR0`）。这类错误在裸机开发中最常见，因为所有外设都是裸地址。**养成习惯：每配置一步，用调试器或打印回读一次寄存器值。**

**链接地址不对导致程序不执行。** 最初用 `ld` 默认地址，bin 烧进去后毫无反应。`-Ttext 0x87800000` 是必须的，因为 BootROM 按这个地址跳转。

**写卡权限。** Windows 下 imxdownload 需要管理员权限直接写物理磁盘；Ubuntu 下不能把卡挂载后再写，要先 `umount`。

## 小结

裸机点灯是「看得见的寄存器」。它把平时被 HAL 库、驱动框架层层包住的四件事——**时钟、复用、方向、电平**——直接摊在眼前。后面写字符设备驱动时，`gpio_direction_output()` 背后其实还是这四步，只是被 gpiolib 封装了。做完这次实验再回头看内核的 `led-gpio` 驱动，能一一对应上。
