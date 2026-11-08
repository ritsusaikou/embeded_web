---
title: Qt 控制 Camera 实验
order: 8
week: 第八次
date: 2026-11-06
summary: 用 V4L2 采集 OV5640 图像并在 Qt 界面实时预览、拍照保存，重点是格式协商、mmap 缓冲队列与像素格式转换。
tags: [Qt, V4L2, OV5640, mmap, 图像采集]
duration: 4 学时
hardware: [i.MX6ULL 开发板, OV5640 摄像头, 触摸屏]
---

## 实验目的

1. 掌握 V4L2 采集的完整流程：查询能力、协商格式、申请缓冲、入队出队、开流停流。
2. 理解 mmap 零拷贝取帧与「缓冲区必须循环归还」的机制。
3. 在 Qt 中把 YUYV 数据转成 `QImage` 实时显示，并支持拍照保存。

## 实验环境

- 摄像头：OV5640 接 CSI 接口，采集节点为 **`/dev/video1`**。
- 内核：已启用 `CONFIG_VIDEO_OV5640`、`CONFIG_VIDEO_MXC_CAPTURE`。

## 原理与要点

V4L2 采集是典型的**队列 + 循环**模型，顺序错了就取不到帧：

```text
open -> QUERYCAP -> S_FMT（协商分辨率与像素格式）
     -> REQBUFS（申请 4 个缓冲）-> QUERYBUF -> mmap -> QBUF（全部入队）
     -> STREAMON -> DQBUF（取一帧）-> 处理 -> QBUF（还回去）-> 循环
     -> STREAMOFF -> munmap -> close
```

三个容易搞错的地方：

- **`/dev/video0` 不是采集节点**。i.MX6ULL 上 `video0` 是 mxc_v4l2 的 output 设备，`video1` 才是 CSI 采集。
- **`S_FMT` 可能被驱动改写**。驱动不一定支持你请求的参数，必须回读 `fmt` 确认实际值，并按实际的 `bytesperline` 计算行跨度。
- **YUYV 一行是 2 字节/像素**，`width * 2 == bytesperline` 时才可以直接整块转换，否则要按行处理，否则画面会斜切。

## 关键代码

```cpp
bool Camera::init(int w, int h)
{
    fd = ::open("/dev/video1", O_RDWR | O_NONBLOCK);
    if (fd < 0) return false;

    v4l2_format fmt{};
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    fmt.fmt.pix.width       = w;
    fmt.fmt.pix.height      = h;
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_YUYV;
    fmt.fmt.pix.field       = V4L2_FIELD_NONE;
    if (ioctl(fd, VIDIOC_S_FMT, &fmt) < 0) return false;

    // 回读实际参数，不要相信请求值
    width  = fmt.fmt.pix.width;
    height = fmt.fmt.pix.height;
    stride = fmt.fmt.pix.bytesperline;

    v4l2_requestbuffers req{};
    req.count  = 4;
    req.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    if (ioctl(fd, VIDIOC_REQBUFS, &req) < 0 || req.count < 2) return false;

    for (unsigned i = 0; i < req.count; ++i) {
        v4l2_buffer buf{};
        buf.type = req.type; buf.memory = req.memory; buf.index = i;
        ioctl(fd, VIDIOC_QUERYBUF, &buf);
        ptrs[i] = mmap(nullptr, buf.length, PROT_READ | PROT_WRITE,
                       MAP_SHARED, fd, buf.m.offset);
        ioctl(fd, VIDIOC_QBUF, &buf);
    }
    v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    return ioctl(fd, VIDIOC_STREAMON, &type) == 0;
}
```

取一帧并转成 QImage（YUYV → RGB888）：

```cpp
QImage Camera::grab()
{
    v4l2_buffer buf{};
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;
    if (ioctl(fd, VIDIOC_DQBUF, &buf) < 0) return QImage();

    const uchar *src = static_cast<uchar *>(ptrs[buf.index]);
    QImage img(width, height, QImage::Format_RGB888);
    for (int y = 0; y < height; ++y) {
        const uchar *line = src + y * stride;
        uchar *dst = img.scanLine(y);
        for (int x = 0; x < width; x += 2) {
            int y0 = line[0], u = line[1] - 128, v = line[3] - 128;
            int y1 = line[2];
            line += 4;
            /* BT.601 定点近似 */
            dst[0] = qBound(0, y0 + ((359 * v) >> 8), 255);
            dst[1] = qBound(0, y0 - ((88 * u + 183 * v) >> 8), 255);
            dst[2] = qBound(0, y0 + ((454 * u) >> 8), 255);
            dst += 3;
            dst[0] = qBound(0, y1 + ((359 * v) >> 8), 255);
            dst[1] = qBound(0, y1 - ((88 * u + 183 * v) >> 8), 255);
            dst[2] = qBound(0, y1 + ((454 * u) >> 8), 255);
            dst += 3;
        }
    }
    ioctl(fd, VIDIOC_QBUF, &buf);   // 必须归还，否则几帧后就没缓冲可用了
    return img;
}
```

界面用 `QTimer` 以 20–30ms 周期取帧刷新 `QLabel`，拍照即把当前 `QImage` `save()` 成 JPEG。

## 实验结果

640×480 分辨率下界面预览流畅（约 15–20 fps，ARM 端纯软件 YUYV 转换是瓶颈），画面色彩正常无斜切；点击拍照在当前目录生成 `photo_时间戳.jpg`，PC 上打开确认清晰。

## 问题与解决

**`open("/dev/video0")` 成功但取不到帧。** 打开的是 output 设备。`v4l2-ctl --list-devices` 确认采集节点是 `video1`，改路径后正常。

**`VIDIOC_S_FMT` 返回 `EINVAL`。** 请求的 1280×720 或 `V4L2_PIX_FMT_RGB565` 不被支持。正确做法是先 `VIDIOC_ENUM_FRAMESIZES` 与 `ENUM_FMT` 枚举能力，再从中挑一个，而不是写死。

**画面斜切、颜色错乱。** 假定了 `bytesperline == width * 2`。实际驱动按 16 字节对齐，`stride` 比 `width*2` 大，必须按 `stride` 取每行起始地址。这个 bug 很隐蔽，因为分辨率是 4 的倍数时一切正常，换成 800×600 才暴露。

**跑十几帧后卡住不再刷新。** 最初忘记在取帧后 `VIDIOC_QBUF` 把缓冲还回队列，4 个缓冲很快耗尽。这个「队列必须循环」的约束是 V4L2 最核心的机制，记住一次就不会再犯。

**帧率太低。** 纯软件 YUYV→RGB 在单核 A7 上开销很大。改用 MJPEG 格式（`V4L2_PIX_FMT_MJPEG`）后由 `QImage::fromData()` 直接解码，帧率明显提升，但相机侧编码会引入额外延迟。

## 小结

这是 8 个实验里最复杂的一个，也是最像「真实产品」的一个：设备枚举、格式协商、内存映射、缓冲队列、像素格式转换、UI 刷新，每一环都可能静默出错。做完之后我形成了一条固定的调试路径：**`v4l2-ctl --all` 看能力 → 抓一帧 `--stream-mmap --stream-to` 存成文件 → 能用播放器打开说明采集链路没问题，问题一定在转换或显示层**。这条路径把「黑盒」变成了可验证的分段。
