# 睡眠监测 — Python 版

`SleepMonitor/`（C# / .NET 8）的 Python 重写，并把心率呼吸算法换成了 C65 固件里的
**BCG 算法**，界面也重做了。原 C# 工程和 `SleepMonitor/web/monitor.html` 未改动。

## 快速开始

Windows 双击 `run.bat`；macOS / Linux 跑 `./run.sh`。首次运行自动建虚拟环境、装依赖、
启动服务并打开浏览器。需要 Python 3.10+，依赖只有 `pyserial` / `numpy` / `aiohttp`。

```bat
run.bat
run.bat list-ports
run.bat selftest
run.bat run --pressure-port COM5 --vitals-port COM4
```

## 传感器协议

心率呼吸口连上后发 ASCII `D` 启动原始 ADC 上报，**6 字节定长帧，200 Hz**：

```
FA 04 ADC1_H ADC1_L ADC2_H ADC2_L
```

两路都是 12 位 ADC（中点 2048）。**只用 ADC2** —— 那是原始 BCG 信号，心跳和呼吸都从
这一路用不同带通分出来；ADC1 实测 std ≈ 2，几乎不动，未使用。

> 早期版本按 `B` 协议的"最低位=通道标志"来拆字节，那在 `D` 模式下会把 ADC 高字节
> 当成数据切进去，是读数不对的根因。现在按帧头同步解析，丢帧会自动重同步。

压力垫口是另一套协议：每帧 `16×32+2 = 514` 字节，收满回一个 `'1' + 查询参数` 的应答。

## 心率 / 呼吸算法（BCG）

移植自 `C65_E103RET6-aidream-cloud/Comm/Analog/bcg/bcg_hrv.c`，替换掉原 `Vitals.cs`：

| 环节 | 心跳 | 呼吸 |
| --- | --- | --- |
| 采样率 | 200 Hz 原速 | 8:1 抽取到 25 Hz |
| 带通 | 0.6–2.5 Hz，4 阶 Butterworth | 0.1–0.5 Hz，4 阶 Butterworth |
| 峰检 | running-max + 抛物线插值 | running-max，最小间隔 2 s |
| 清洗 | ±15% 局部中位数 → \|ΔRR\|>150ms | 周期 ±30% 中位数 |
| 输出 | HR = 60000 / median(RR)，clamp 42–110 | BR = 60000 / median(T)，clamp 6–30 |
| 稳定化 | 半/倍频纠正 + 滑动中位数 + 跳变限幅 12 bpm | — |
| 附加 | SDNN / RMSSD / pNN50 / SQI | 呼吸暂停检测 / SQI |

滤波器系数直接沿用固件里 scipy 算好并验证过的那组，因此**不改系数、把信号喂到它
原生的 200 Hz / 25 Hz 上**。窗口 60 秒，至少攒够 30 秒才出第一个读数，之后每 2 秒
重算一次（一次全窗计算约 5 ms）。

**页面波形和检测器共用同一组带通系数**（各自独立的滤波器状态），所见即算法所见。

自检里有合成信号验证：

```
合成 72bpm/15次分 → 检出 心率=72.1 呼吸=14.9 (SQI 90/75)
```

### SQI 怎么看

`SQI` 是 0–100 的信号质量分。心率的三项里 **自相关项最能反映真实质量**：按固件标定，
噪声 / 空床 ≈ 0，干净准周期心跳 ≈ 0.4–0.7。如果 SQI 长期低于 30，先查传感器位置和增益，
不要直接信 BPM。

## 在离床判定

用压力垫做，不再依赖生命体征信号的功率：

1. 16×32 卡尔曼滤波后的图里，单个测点 > `--on-bed-cell`（默认 30）记 1 分
2. 总分 ≥ `--on-bed-count`（默认 40）判为有人
3. 带迟滞：连续 3 帧成立才进入在床，连续 15 帧不成立才判离床（防翻身闪断）

**离床时心率呼吸直接显示 `--`，波形照常刷新。**

两个阈值可以在**页面上直接改**，改完即时生效；点「保存」写入 `python/settings.json`，
换浏览器、重启程序都还在。页面实时显示当前受压测点数，方便标定：空垫记一次读数，
躺上去再记一次，阈值取中间值。

配置优先级：命令行参数 > `settings.json` > 内置默认值。

## 界面

参考 `handpress v3` 的设计语言：纯黑底、白字、半透明玻璃面板、单一橙色强调
（`#ff7a1a`，只用在开关、保存反馈和呼吸波形）。

左边 3D 压力场沿用原版结构（缓慢来回运镜 + OrbitControls + 自动旋转开关），
改动只有：床体去掉实心面片**只保留线框**、测点由方块换成**深灰小球**（Lambert 材质不反光，
直径随压力从 1 倍长到 2 倍）、
配色换成手套那套色标（`#ff4fb8 → #d93cff → #b02cff → #ff2f6d → #ff6a28 → #ffe86a`）、
下陷幅度降到原来的 1/3、灯光整体提亮。

波形是**固定量程**（心跳 ±24、呼吸 ±12），不自适应缩放，超范围裁剪在边界上，
画布中线是零位基准。传感器增益或安装方式变了觉得波形太平/顶格，改
`python/web/monitor.html` 顶部的 `HEART_WAVE_RANGE` / `RESP_WAVE_RANGE` 即可。

性能上加了脏标记：4608 个实例只在收到新压力帧（5 Hz）时重建，不再每帧重算 —— 这是
之前右侧波形卡顿的主因。波形推送提到 **20 Hz**，每次只前进 1–2 个点，视觉上连续。

页面在 `python/web/monitor.html`，C# 版仍用它自己的 `SleepMonitor/web/monitor.html`。

## 命令

| 命令 | 作用 |
| --- | --- |
| `run` | 启动监测程序（默认命令，可省略） |
| `list-ports` | 列出串口并标出自动识别到的两个角色 |
| `selftest` | 自检：依赖 / 页面 / 串口 / 算法 |

`run` 参数：`--pressure-port` `--vitals-port` `--query-parameter` `--host` `--port`
`--web-root` `--on-bed-cell` `--on-bed-count` `--no-browser` `--keep-alive`。

`--port` / `--query-parameter` / `--on-bed-cell` / `--on-bed-count` 不指定时会读 `settings.json`。

## 页面上能改的设置

| 设置 | 生效时机 |
| --- | --- |
| 压力查询参数 | 即时 |
| 在离床两个阈值 | 即时 |
| 压力垫 / 心率呼吸串口 | 点「应用串口」即时重连 |
| 服务端口 | 保存后**重启**生效 |

点「保存」写入 `python/settings.json`（串口、端口、阈值、查询参数全部一起存）。
**服务端才是设置的权威来源**：页面连上后由服务端下发当前值填进输入框，页面不做本地缓存，
所以换个浏览器打开不会用旧值覆盖掉服务端配置。

记住的串口如果因为换设备/换 USB 口不存在了，会自动退回自动识别并在日志里提示。

「退出程序」按钮可以干净地关掉服务并释放串口。端口被占用时启动会给出明确提示和处理办法，
不再是一串 traceback。

## 模块对照

| C# | Python | 说明 |
| --- | --- | --- |
| `Program.cs` | `server.py` + `monitor.py` | aiohttp 静态服务 + `/ws`，看门狗 |
| `MonitorService.cs` | `monitor.py` | 采集编排与广播 |
| `SerialReader.cs` | `readers.py` | 两路串口读取线程 + 帧解析 |
| `Vitals.cs` + `SignalProcessing.cs` | **`bcg.py`**（换成 C65 BCG） | 心率 / 呼吸 |
| `MonitorService.cs`（热图） | `pressure.py` + `kalman.py` | 卡尔曼 → ×3 → 高斯 → 在离床 |
| `GetCommPorts`（SetupDi） | `serial_ports.py` | 串口角色识别，跨平台 |

## 注意事项

**macOS 的 5000 端口**：ControlCenter（隔空播放接收器）默认占用 `*:5000`。本程序绑
`127.0.0.1:5000` 通常不冲突；报占用就用 `--port 5001`。

**串口角色**：CP2105 双路 UART，Enhanced（macOS 上尾号 `0`）→ 心率呼吸，
Standard（尾号 `1`）→ 压力垫。认错了用 `--pressure-port` / `--vitals-port` 指定。

**压力垫帧率**：下位机收到应答后约 188 ms 才吐下一帧，即 ~5 fps，不是软件瓶颈
（514 字节在 115200 下只占 45 ms）。
