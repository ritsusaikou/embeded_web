---
title: Qt 文本读写实验
order: 6
week: 第六次
date: 2026-10-29
summary: 第一次把 Qt 应用交叉编译到开发板运行：QFile + QTextStream 做打开、编辑、保存，重点解决库依赖与中文编码两个必踩的坑。
tags: [Qt, 交叉编译, QFile, 文件IO]
duration: 4 学时
hardware: [i.MX6ULL 开发板, 触摸屏]
---

## 实验目的

1. 掌握 Qt 工程的交叉编译流程（qmake → make → 部署）。
2. 使用 `QFile` / `QTextStream` 完成文本文件的打开、编辑、保存。
3. 解决嵌入式 Qt 应用最常见的两类问题：动态库依赖、字符编码。

## 实验环境

- Qt 5.12.9（ARM 版，厂商提供），宿主机 Qt Creator 配置对应 Kit。
- 板子通过 `linuxfb` 平台插件直接绘制到 `/dev/fb0`，无 X11 / Wayland。
- 交叉编译产物通过 NFS 目录直接在板子上运行，避免反复拷贝。

## 原理与要点

Qt 应用在板子上跑不起来，90% 不是代码问题，而是**运行环境问题**，主要两块：

- **库依赖**：交叉编译时链接的是宿主机 Qt 库，板子上必须有对应版本的 `.so`，否则报 `error while loading shared libraries`。
- **平台插件**：没有桌面环境时必须显式指定 `QT_QPA_PLATFORM=linuxfb`，否则 Qt 找不到 `libqlinuxfb.so` 直接退出。

`QTextStream` 默认编码在不同 Qt 版本行为不一致，读写中文必须显式 `setCodec("UTF-8")`。

## 关键代码

```cpp
// 打开
void MainWindow::on_btnOpen_clicked()
{
    QString path = QFileDialog::getOpenFileName(
        this, "打开文本", "/root", "文本文件 (*.txt);;所有文件 (*)");
    if (path.isEmpty()) return;

    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        QMessageBox::warning(this, "打开失败", file.errorString());
        return;
    }
    QTextStream in(&file);
    in.setCodec("UTF-8");
    ui->textEdit->setPlainText(in.readAll());
    file.close();
    m_currentPath = path;
}

// 保存
void MainWindow::on_btnSave_clicked()
{
    QFile file(m_currentPath.isEmpty()
        ? QFileDialog::getSaveFileName(this, "保存", "/root/note.txt", "文本 (*.txt)")
        : m_currentPath);
    if (file.fileName().isEmpty()) return;
    if (!file.open(QIODevice::WriteOnly | QIODevice::Text | QIODevice::Truncate)) {
        QMessageBox::warning(this, "保存失败", file.errorString());
        return;
    }
    QTextStream out(&file);
    out.setCodec("UTF-8");
    out << ui->textEdit->toPlainText();
    file.close();
    statusBar()->showMessage("已保存：" + file.fileName(), 3000);
}
```

```bash
/opt/Qt5.12.9/arm/bin/qmake textedit.pro
make -j4
cp textedit /home/user/nfs/rootfs/root/
# 板子上：
cd /root && ./textedit -platform linuxfb
```

## 实验结果

程序在开发板 4.3 寸屏上正常启动，能选择文件、显示内容、编辑后保存；重新打开中文内容不乱码；保存后用 `cat` 在串口里查看文件内容与界面一致。

## 问题与解决

**`error while loading shared libraries: libQt5Widgets.so.5`。** 板子 rootfs 里没有 Qt 运行库，或版本不匹配。把工具链对应版本的 `lib` 目录整体拷到板子 `/usr/lib/`，或临时 `export LD_LIBRARY_PATH=/opt/qt/lib`。注意**不要混用宿主机 x86 的库**，用 `file libQt5Widgets.so.5` 确认是 ARM ELF。

**中文全是方块或乱码。** 两个层面：文件读写层用 `setCodec("UTF-8")`；界面层要确认 rootfs 里有中文字体（`/usr/share/fonts`），否则即使编码正确也只能显示方块。本次是缺字体，补一个 `wqy-zenhei` 后正常。

**`qmake` 用错版本。** 终端里默认 `qmake` 是宿主机 x86 版本，编出来的程序在板子上直接 `Exec format error`。必须用**绝对路径**调用 ARM 版 qmake，或用 Qt Creator 的 Kit 明确指定。

**没有 X，窗口不出来。** 加 `-platform linuxfb` 或设环境变量 `QT_QPA_PLATFORM=linuxfb:fb=/dev/fb0`。触摸屏不响应则要配 `QT_QPA_GENERIC_PLUGINS=tslib:/dev/input/event1`。

## 小结

这是第一次把「自己写的 GUI」真正跑在板子上，成就感很强。但更值得记的是那套排查顺序：**先 `file` 看架构 → 再 `ldd` 看依赖 → 再 `QT_DEBUG_PLUGINS=1` 看插件加载 → 最后才怀疑代码**。这套方法在后面的 LED、Camera 实验里被反复使用。
