---
title: 嵌入式 Linux 音频设备驱动实验
order: 5
week: 第五次
date: 2026-10-22
summary: 理解 ASoC 的 Machine / Platform / Codec 三层结构，配好 WM8960 的设备树 sound 节点，用 ALSA 完成一次录音与回放。
tags: [ASoC, WM8960, ALSA, 设备树, I2S]
duration: 4 学时
hardware: [i.MX6ULL 开发板, WM8960 Codec, 耳机, 麦克风]
---

## 实验目的

1. 理解 ASoC（ALSA System on Chip）的分层设计：Machine、Platform、Codec 各自职责。
2. 掌握音频链路上「控制通道」与「数据通道」的区别：I2C 配寄存器、SAI/I2S 走音频流。
3. 通过设备树描述板级连接，用 `aplay` / `arecord` / `amixer` 验证驱动。

## 实验环境

- Codec：WM8960，I2C2 控制（地址 `0x1a`），SAI2 传数据，MCLK 24MHz。
- 内核配置需打开：`CONFIG_SND_SOC_WM8960`、`CONFIG_SND_SOC_FSL_SAI`、`CONFIG_SND_SOC_IMX_WM8960`。

## 原理与要点

音频在 Linux 里被拆成三层，第一次看很容易晕，其实各管一件事：

| 层次 | 负责 | 在本实验中的实体 |
| --- | --- | --- |
| Platform（CPU 侧） | 把内存里的 PCM 数据搬到音频接口 | `sai2` DAI，DMA 搬运 |
| Codec（芯片侧） | 数模转换、音量、通路开关 | WM8960，I2C 配置寄存器 |
| Machine（板级） | 把上面两者连起来，声明板上有哪些器件 | 设备树 `sound` 节点 |

设备树里的 `audio-routing` 决定「声音从哪个物理口出来」，如果不写或者写错，驱动照样加载成功，但耳机没声——这是音频调试里最典型的「静默失败」。

## 关键代码

```dts
sound {
    compatible = "fsl,imx-audio-wm8960";
    model = "wm8960-audio";
    audio-cpu = <&sai2>;
    audio-codec = <&codec>;
    audio-routing =
        "Headphone Jack", "HP_L",
        "Headphone Jack", "HP_R",
        "Ext Spk", "SPK_LP",
        "Ext Spk", "SPK_LN",
        "Ext Spk", "SPK_RP",
        "Ext Spk", "SPK_RN",
        "LINPUT1", "Main Mic",
        "MICB", "Main Mic";
    hp-det-gpios = <&gpio5 5 GPIO_ACTIVE_LOW>;
    mux-int-port = <2>;
    mux-ext-port = <3>;
};
```

```bash
cat /proc/asound/cards          # 应出现 wm8960-audio
aplay -l                        # 列出 playback 设备
amixer -c 0 sset 'Headphone' 100% on
amixer -c 0 sset 'Speaker'   100% on
aplay -f cd /root/test.wav      # 16bit 44.1kHz 立体声
arecord -d 5 -f cd -r 44100 /root/rec.wav
aplay /root/rec.wav
```

## 实验结果

`/proc/asound/cards` 正确识别声卡，耳机里能听到测试音频；录音 5 秒回放清晰可辨，采样率 44.1kHz、双声道、无爆音。`amixer contents` 可见 DAC / ADC / HP / SPK 各路增益均可调。

## 问题与解决

**驱动加载成功但完全没声音。** 这是本次耗时最长的问题，排查分三步：先 `cat /proc/asound/cards` 确认声卡存在；再 `amixer contents` 发现 DAC 与 HP 通路都是 `off`；最后把 `Headphone`、`Speaker`、`Playback` 逐个 `on` 并调增益，声音出现。结论：**Codec 默认是静音状态，驱动 probe 成功 ≠ 出声**。

**`i2cdetect` 扫不到 0x1a。** 说明 CPU 根本没和 Codec 通信。检查两点：I2C2 的 pinctrl 是否配好、MCLK 是否真的在输出（SAI2 的 `mclk` 需要 `assigned-clocks` 配置），MCLK 缺失时 Codec 内部 PLL 起不来，自然不应答。

**录音有杂音、播放速度不对。** 采样率不匹配。`aplay` 默认参数与 wav 头不一致时会重采样并产生杂音，显式指定 `-f cd -r 44100 -c 2` 后正常。速度不对则是 MCLK 与 sysclk 配比问题，与采样率参数无关。

**耳机检测极性反了。** `hp-det-gpios` 的 `GPIO_ACTIVE_LOW` 与实际电路相反，导致插耳机时系统认为在拔出状态、切到扬声器。对照原理图改极性即可。

## 小结

音频驱动看起来复杂，但一旦接受「三层各管一件事」的划分，就看得很清楚：数据走 DMA 到 SAI 到 Codec，控制走 I2C 到 Codec 寄存器，板级差异全在 `sound` 节点里。这次实验也让我第一次体会到**设备树写错不会报错、只会不工作**这件事的威力——后来我给整个 dts 加了注释，把每个 `audio-routing` 对应的物理接口标上。
