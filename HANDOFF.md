# HANDOFF — 睡眠监测 Python 版交接文档

> 写给下一个接手的 AI（或人）。目标：读完这份文档，不需要翻聊天记录就能继续干活。
> 最后更新：2026-08-07，最新提交 `8009146`。

## 一、这是什么

床垫睡眠监测上位机：两路 USB 串口读**压力垫**（16×32 阵列）和**心率呼吸传感器**（BCG），
本地起 aiohttp 服务，浏览器页面显示 3D 压力场 + 心率/呼吸数值和实时波形。

- 仓库：`http://gitea.mirahome.net/Production/AirMattress.git`（匿名可 clone，push 免认证）
- 工作分支：**`python`**（基于 `origin/sleep-monitor`），所有 Python 代码在 `python/` 目录
- 本地路径：`~/Documents/文稿 - Will的MacBook Air/claude src/aidream-press`
- C# 原版在 `SleepMonitor/`，**未改动**，仍在用旧算法和旧页面，两版可并存

分支史一句话：`master` 上还有个更大的 C#/WinForms 项目 `bletest/`（带 BLE 气囊控制、
门店注册、OSS 上传），最初的 Python 移植做的是它，后来用户明确说只要 `sleep-monitor`
分支这个精简版，`python` 分支已整体重建，`bletest` 的移植已废弃。

## 二、怎么跑

```bash
cd python
./run.sh                    # macOS/Linux；Windows 双击 run.bat
./run.sh selftest           # 自检：依赖/页面/串口/算法（合成信号 72bpm/15次分）
./run.sh list-ports         # 看串口识别结果
./run.sh run --no-browser --keep-alive   # 调试常用（不开浏览器、无连接不退出）
```

run 脚本自动建 `.venv` 并装依赖（只有 pyserial / numpy / aiohttp）。
页面地址 `http://localhost:5000/monitor.html`。
本机可用的解释器：`python3.13`（Homebrew）；系统 `python3` 是 3.9，太旧。
mac 上跑 run.sh 要 `PYTHON_BIN=python3.13 ./run.sh`（首次建 venv 时）。

## 三、硬件与协议（最重要，都是实测出来的）

### 串口

CP2105 双路 USB-UART，**波特率都是 115200**。角色固定：

| 接口 | macOS 设备名尾号 | Windows 友好名 | 角色 |
|---|---|---|---|
| Enhanced | `…0` | 含 "Enhanced" 或 "A CH342" | 心率呼吸 |
| Standard | `…1` | 含 "Standard" 或 "B CH342" | 压力垫 |

⚠️ 用户会换设备/换 USB 口，串口序列号会变（见过 `01F169DF` → `01F6E39C` → `01F6E3B8`）。
自动识别逻辑在 `sleepmonitor/serial_ports.py`，settings.json 里记住的口不存在时会自动回退。
桌上还接了一个 SEGGER J-Link 和一个 CP2102N，与本项目无关，别认错。

### 心率呼吸口 —— `D` 协议

打开后发 ASCII `D`，之后下位机持续吐 **6 字节定长帧，200Hz**：

```
FA 04 ADC1_H ADC1_L ADC2_H ADC2_L
```

- 两路都是 12 位 ADC，中点 2048
- **只用 ADC2**（原始 BCG 信号，std≈14）；ADC1 几乎不动（std≈2），不用
- 送算法前 `adc2 - 2048`
- 解析必须按 `FA 04` 帧头同步，丢帧要能重同步（`readers.py` 已实现）

⚠️ **历史大坑 #1**：还有一个 `B` 协议（旧版 C# 用的），约定"字节最低位=通道标志"。
`D` 模式下如果还按 B 的方式逐字节拆，会把 ADC 高字节当数据切进去 —— 这曾是
"读数不对"的真正根因。别再走回去。

⚠️ **历史大坑 #2**：pyserial `read(4096)` 配 1s 超时会攒满 1 秒才返回（流速只有
~1.2KB/s），页面波形一秒跳一格。正确姿势是阻塞 `read(1)` 后立刻带走 `in_waiting`
的全部（`8009146` 修的）。

### 压力垫口

打开后发 `"11"` 启动；每帧 **514 字节**（2 字节帧头 `FF xx` + 512 字节 = 16×32 点，
`frame[j*16+i+2]` 布局）；收满一帧回发 2 字节应答 `'1' + 查询参数`（默认 0x05），
下位机收到应答后**约 188ms** 才吐下一帧 → **~5fps 是下位机节奏，不是软件瓶颈**，别去优化。

## 四、算法（BCG）

来源：`~/Documents/文稿 - Will的MacBook Air/claude src/x100-aidream/C65_E103RET6-aidream-cloud/Comm/Analog/bcg/bcg_hrv.{c,h}`
（用户指定用这份，同名文件还有两个旧版本在别的目录，别拿错）。
移植在 `sleepmonitor/bcg.py`，逐行对齐 C 实现。

链路：ADC2@200Hz → 心跳走 0.6–2.5Hz 四阶 Butterworth（200Hz 原速）；
呼吸 8:1 均值抽取到 25Hz → 0.1–0.5Hz 带通。之后 running-max 峰检 + 抛物线插值 →
RR 两段清洗（±15% 局部中位数、|ΔRR|>150ms）→ HR=60000/median(RR)。
跨窗有半/倍频纠正 + 滑动中位数 + 跳变限幅 12bpm（`HrTracker`）。
窗口 60s，攒满 30s 出第一个数，之后每 2s 重算（一次约 5ms）。

**不能乱动的约定**：

- 滤波器系数是 scipy 针对 200/25Hz 算好并在固件上验证过的，**改采样率必须重算系数**，
  改系数不如把信号重采样到 200/25Hz
- `_median` 是"排序取 `a[n//2]`"（偶数个取偏右，不插值），与固件一致，别换成 statistics.median
- HR clamp 42–110 bpm、呼吸 clamp 6–30，是固件设定；用户如需更宽范围要做成可配置
- 页面波形和检测器**共用同一组带通系数**（各自独立滤波器状态）——"所见即算法所见"
  是用户明确要的，别退回 EMA

验证基准（三平台一致，写死在 selftest 里）：
合成 72bpm/15次分 → 检出 **72.1 / 14.9，SQI 90/75**；压力随机帧 → 4608 点，峰值 13.91。
改算法后这几个数变了就是引入了行为差异。

## 五、在离床判定

压力垫做：卡尔曼后 16×32 图里单点 > `onBedCell`（当前 25）记 1 分，总分 ≥
`onBedCount`（当前 55）判有人；迟滞进 3 帧/出 15 帧。离床时 BPM 显示 `--`，**波形保留**。
逻辑在 `pressure.py::_update_occupancy`。

## 六、配置体系

优先级：**页面手动选的 > 命令行参数 > `python/settings.json` > 内置默认**。

- `settings.json` 由页面「保存」按钮写入（qp、双阈值、两个串口、服务端口），已 gitignore
- **服务端是权威**：页面连上后由服务端下发填表，页面不用 localStorage
  （⚠️ 历史坑 #3：旧页面用 localStorage 恢复并在 onopen 时上推，会把服务端配置覆盖掉，
  已删除，别加回来）
- 改串口 → 页面「应用串口」即时重连；改服务端口 → 保存后重启生效
- 页面「退出程序」可远程关掉进程释放串口；端口被占时启动报友好错误（不是 traceback）

## 七、页面（`python/web/monitor.html`，自包含单文件 + libs/three.min.js）

设计语言是用户逐条定的，**改样式前先看这份清单**：

- 参考 `~/Desktop/桌面 - Will的MacBook Air/handpress v3/index.html`：纯黑底、玻璃面板、
  白字、单一橙色强调 `#ff7a1a`。用户明确说**不要花花绿绿、不要格子背景**
- 压力配色：**严格 matplotlib plasma**（64 锚点内嵌在 JS 里，误差 1.48/255），
  色标条与 3D 共用同一数组
- 3D：沿用原 C# 版结构（来回运镜 + OrbitControls + 自动旋转开关）。床体**只留线框**；
  测点是**深灰 (0x1c1c1e) Lambert 小球（不反光）**，半径 0.085，直径随压力 1→2 倍，
  下陷幅度 `0.4/3`（用户要求降到 1/3）
- 波形：**固定量程**（心跳 ±24、呼吸 ±12，页面顶部常量），不自适应；超范围裁剪；
  中线画零位。窗口：心跳 4s@100Hz=400 点，呼吸 8s@25Hz=200 点
- 性能：4608 个实例只在新压力帧（5Hz）到达时重建（脏标记）；波形每帧重画；
  数据推送 20Hz（`monitor.py::BROADCAST_INTERVAL=0.05`）

WebSocket 协议：下行 `pressure`（base64 float32×4608）/ `sensors`（hr、br、波形、
occupied、cells、SQI）/ `serial`（连接状态+串口列表+设置）/ `saved` / `bye`；
上行 `{type:"control", action: toggleSerial | setPressureQueryParameter |
setOnBedThresholds | setPorts | setServerPort | listPorts | saveSettings | shutdown}`。

## 八、验证手段

- `selftest`：上面说的合成信号基准
- 真机：设备就插在这台 mac 上，`--no-browser --keep-alive` 跑起来用浏览器看
- **Windows 验证走 willpc**：`ssh willpc`（Cloudflare Tunnel，免密，见
  `~/.claude/skills/windows-remote`）。流程：`git archive python | tar` 导出干净树 →
  scp 到 `C:\tmp\` → 跑 `run.bat selftest`。⚠️ willpc 上 git clone gitea 会被凭据
  管理器卡住（401），直接传文件最省事。中文输出经 ssh 会乱码，落盘 UTF-8 文件再取
- 抓原始数据分析：见 git 历史里的 probe 脚本思路（pyserial 直读存 bin，numpy 分析）

## 九、已知未完事项（按优先级）

1. **信号质量偏弱待排查**：120s 实测 SQI 仅 26–36，自相关项 0.00–0.08（固件标定：
   空床≈0，干净心跳 0.4–0.7），峰检 67 拍清洗掉 40 拍。HR/BR 数值合理但置信度低。
   需要用户确认躺床采集一次，如确认有人则是传感器位置/增益问题，不是算法问题
2. **阈值与量程待真人标定**：在离床 25/55、波形量程 ±24/±12 都是单人单次实测定的。
   页面实时显示受压测点数，让用户空垫/躺床各记一次读数再定
3. C# 版（`SleepMonitor/`）仍是旧算法旧页面，用户没要求同步；若要同步需把 BCG 移回 C#
4. macOS 上 ControlCenter 占 `*:5000`（隔空播放），本程序绑 127.0.0.1 通常无冲突，
   报占用换 `--port`

## 十、和这位用户协作的经验

- 反馈是**增量、口语化、随时插进来的**（经常在你干活时进来一句），逐条记账逐条确认，
  最后对总账；他说"搞错了"就是要换方向，别恋战旧方案
- 审美："别创新"——他指名的参考（handpress、plasma、原版 3D 结构）照抄就好，
  自由发挥会被打回
- 他懂硬件和信号：会直接给帧格式（"FA 04 ADC1_H…你取 adc2 就行"）、指定算法来源
  （"参考我的 c65-e103ret6 里的 bcg"），这些信息优先级最高，比你的推断可靠
- 有性能观感问题（"卡""慢"）先量化再改：两次"波形卡"根因都不在渲染
  （一次是每帧重建 4608 实例，一次是串口攒批）
- 提交信息用中文，风格看 `git log`；每轮改完要 lint（pyflakes）+ selftest + 真机跑通
  再 push 到 `origin/python`
