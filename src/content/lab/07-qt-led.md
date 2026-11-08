---
title: Qt 控制 LED 实验
order: 7
week: 第七次
date: 2026-11-03
summary: 用 Qt 界面操作真实硬件：通过 sysfs 的 brightness 节点控制内核 led 驱动，并加定时器做闪烁、滑块调频率。
tags: [Qt, sysfs, led驱动, 信号槽]
duration: 4 学时
hardware: [i.MX6ULL 开发板, LED0, 触摸屏]
---

## 实验目的

1. 打通「GUI 事件 → 系统调用 → 内核驱动 → 硬件引脚」这条完整链路。
2. 掌握通过 sysfs 操作内核设备节点的通用方法。
3. 使用 `QTimer`、`QSlider` 与信号槽实现闪烁与频率调节。

## 实验环境

- 内核已启用 `led-gpio` 驱动，LED0 对应 `/sys/class/leds/sys-led/`。
- Qt 5.12.9 ARM 版，`linuxfb` 平台插件。

## 原理与要点

Qt 本身不直接碰寄存器，最省事也最通用的路径是 **sysfs**：内核的 led 驱动把每个 LED 抽象成目录，里面有 `brightness`（读写亮度）和 `trigger`（触发方式）两个关键文件。

```text
/sys/class/leds/sys-led/
├── brightness    # 写 1 亮 / 0 灭
├── max_brightness
└── trigger       # none / heartbeat / timer / mmc0 ...
```

**关键细节**：`trigger` 不是 `none` 时（比如 `heartbeat`），手动写 `brightness` 会被驱动覆盖，看起来像「写了没反应」。所以第一步必须是 `echo none > trigger`。

界面层用 `QTimer` 定时翻转状态实现闪烁，用 `QSlider` 动态改 `setInterval()`。

## 关键代码

```cpp
#define LED_PATH "/sys/class/leds/sys-led/brightness"

bool MainWindow::setLed(bool on)
{
    QFile f(LED_PATH);
    if (!f.open(QIODevice::WriteOnly)) {
        statusBar()->showMessage("权限不足：" + f.errorString(), 3000);
        return false;
    }
    f.write(on ? "1" : "0");
    f.close();
    return true;
}

void MainWindow::on_btnOn_clicked()  { setLed(true);  ui->lblState->setText("亮"); }
void MainWindow::on_btnOff_clicked() { setLed(false); ui->lblState->setText("灭"); }

// 闪烁
void MainWindow::on_btnBlink_clicked()
{
    if (m_blinkTimer->isActive()) {
        m_blinkTimer->stop();
        setLed(false);
        ui->btnBlink->setText("开始闪烁");
    } else {
        m_blinkTimer->start(ui->sliderFreq->value());
        ui->btnBlink->setText("停止闪烁");
    }
}

// 滑块调频率：毫秒，越小越快
void MainWindow::on_sliderFreq_valueChanged(int v)
{
    m_blinkTimer->setInterval(v);
    ui->lblFreq->setText(QString("%1 ms").arg(v));
}
```

初始化时先解除触发方式：

```cpp
QFile t("/sys/class/leds/sys-led/trigger");
if (t.open(QIODevice::WriteOnly)) { t.write("none"); t.close(); }
```

## 实验结果

点击按钮 LED0 立即响应，无可见延迟；开启闪烁后按滑块设定的 100–1000ms 周期稳定闪烁；串口里 `cat /sys/class/leds/sys-led/brightness` 与界面状态始终一致。

## 问题与解决

**写 `brightness` 没反应。** 先怀疑权限，`ls -l` 显示 `-rw-r--r-- root root`，普通用户确实写不了；`chmod 666` 后可以写，但仍不亮——最后发现 `trigger` 是 `timer`，驱动在自动翻转。改成 `none` 立即正常。两个问题叠在一起，容易只解决一半就以为方法错了。

**节点名不是 `sys-led`。** 我按记忆写了 `led0`，实际板子上的命名由设备树 `label` 决定。正确做法是先 `ls /sys/class/leds/` 再写代码，不要猜。

**界面上点了按钮但程序卡住。** 早期版本在按钮回调里写了 `while(1)` 做闪烁，直接阻塞事件循环，窗口失去响应。改用 `QTimer` 异步驱动后正常——这是 GUI 编程的基本纪律：**回调里不做长耗时/阻塞操作**。

**权限方案。** 交付时不能每次都手动 `chmod`。用 udev 规则在设备创建时设权限，或者干脆让应用以 root 运行（开发阶段可以，正式产品应避免）。

## 小结

这是整个课程里「软硬结合」感觉最强的一次：屏幕上的一个点击，最终变成引脚上的一次电平翻转。它把前四次实验串了起来——裸机寄存器、字符设备驱动、sysfs 抽象、Qt 应用，四层叠在一起终于跑通。也正因为走通了全链路，我才理解为什么 Linux 要用 sysfs 这种「文件即接口」的设计：用户态不需要任何头文件、不需要知道驱动怎么写的，就能控制硬件。
