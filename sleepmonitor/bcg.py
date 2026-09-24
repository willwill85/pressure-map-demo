"""BCG 心率 / 呼吸算法, 移植自 C65_E103RET6 固件的 ``Comm/Analog/bcg/bcg_hrv.c``。

替换掉原 ``Vitals.cs`` 那套检测逻辑。相比旧算法的改进:

* 4 阶 Butterworth 带通把心跳(0.6-2.5Hz)和呼吸(0.1-0.5Hz)真正分离出来,
  旧实现只有一级 EMA, 截止频率约 17Hz, 噪声几乎原样进检测器
* running-max 峰检 + 抛物线插值, 亚采样精度 ~0.5ms
* RR 两段清洗(±15% 局部中位数 / |ΔRR|>150ms) + 中位数求 HR, 抗单拍误检
* 跨窗半/倍频纠正 + 滑动中位数 + 跳变限幅, 消除整夜尖刺
* SQI 信号质量评分, 呼吸暂停检测

**采样率**: 下位机 ``D`` 协议就是 200Hz, 正好是固件滤波器系数的设计速率, 所以
心跳链路直接原速用; 呼吸链路 8:1 抽取到 25Hz, 也和固件 ``resp_get_batch`` 一致。
滤波器系数、峰检间距、RR 接受区间、半倍频阈值那一整套参数因此原样可用。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

# ---- 心跳参数 (bcg_hrv.h) ----
BCG_FS = 200                    # 算法内部采样率
BCG_WIN_SEC = 60
BCG_WIN_N = BCG_FS * BCG_WIN_SEC
BCG_MIN_SEC = 30                # 至少 30s 才算
BCG_RR_MAX = 320
BCG_HR_MIN_BPM = 42
BCG_HR_MAX_BPM = 110
BCG_HR_INIT_BPM = 75.0
BCG_RR_ACCEPT_MIN_MS = 60000.0 / (BCG_HR_MAX_BPM + 20.0)   # ~461ms
BCG_RR_ACCEPT_MAX_MS = 60000.0 / (BCG_HR_MIN_BPM - 2.0)    # ~1500ms
BCG_HR_DOUBLE_LO, BCG_HR_DOUBLE_HI = 1.7, 2.3
BCG_HR_HALF_LO, BCG_HR_HALF_HI = 0.43, 0.58
BCG_HR_HIST_N = 7
BCG_HR_MAX_STEP_BPM = 12.0

# ---- 呼吸参数 ----
BCG_RESP_FS = 25
BCG_RESP_WIN_N = BCG_RESP_FS * BCG_WIN_SEC
BCG_RESP_MIN_BPM = 6
BCG_RESP_MAX_BPM = 30
BCG_RESP_APNEA_SEC = 10
BCG_RESP_MIN_WIN_SEC = 30
BCG_RESP_BR_MAX = 64


class Biquad:
    """Transposed Direct Form II 双二阶节。"""

    __slots__ = ("b0", "b1", "b2", "a1", "a2", "z1", "z2")

    def __init__(self, b0: float, b1: float, b2: float, a1: float, a2: float) -> None:
        self.b0, self.b1, self.b2, self.a1, self.a2 = b0, b1, b2, a1, a2
        self.z1 = self.z2 = 0.0

    def reset(self) -> None:
        self.z1 = self.z2 = 0.0

    def tick(self, x: float) -> float:
        y = self.b0 * x + self.z1
        self.z1 = self.b1 * x - self.a1 * y + self.z2
        self.z2 = self.b2 * x - self.a2 * y
        return y


def heart_bandpass() -> tuple[Biquad, Biquad]:
    """0.6-2.5Hz @200Hz, 4 阶 Butterworth (2 级 SOS)。系数照抄 bp_init()。

    固件里标注的实测幅频: 0.6Hz -3.4dB / 1.0Hz -0.0dB / 2.0Hz -0.7dB / 2.5Hz -3.0dB
    """
    return (
        Biquad(8.544256953e-04, 1.708851391e-03, 8.544256953e-04, -1.932961056, 9.375894971e-01),
        Biquad(1.0, -2.0, 1.0, -1.979772309, 9.802264261e-01),
    )


def resp_bandpass() -> tuple[Biquad, Biquad]:
    """0.1-0.5Hz @25Hz, 2 级 SOS。系数照抄 resp_core()。"""
    return (
        Biquad(2.357208773e-03, 4.714417546e-03, 2.357208773e-03, -1.881324490, 8.929939581e-01),
        Biquad(1.0, -2.0, 1.0, -1.970622900, 9.714199362e-01),
    )


def _median(values: list[float]) -> float:
    """与固件一致: 排序后取 a[n//2] (偶数个时取偏右那个, 不做插值)。"""
    return sorted(values)[len(values) // 2]


def _parabolic_delta(y_prev: float, y_peak: float, y_next: float) -> float:
    denom = y_prev - 2.0 * y_peak + y_next
    if denom == 0.0:
        return 0.0
    d = 0.5 * (y_prev - y_next) / denom
    return max(-0.5, min(0.5, d))


@dataclass
class HeartResult:
    ready: bool = False
    hr_bpm: float = 0.0
    hr_bpm_raw: float = 0.0
    mean_rr_ms: float = 0.0
    sdnn_ms: float = 0.0
    rmssd_ms: float = 0.0
    pnn50_pct: float = 0.0
    n_rr: int = 0
    n_rr_raw: int = 0
    n_samples_used: int = 0
    sqi: float = 0.0
    sqi_coverage: float = 0.0      # aclag: 信号自相关周期性
    sqi_survival: float = 0.0      # art:   无伪迹 RR 占比
    sqi_regularity: float = 0.0    # rreg:  节律规整度


@dataclass
class RespResult:
    ready: bool = False
    rr_bpm: float = 0.0
    mean_period_ms: float = 0.0
    n_breaths: int = 0
    n_breaths_raw: int = 0
    n_samples_used: int = 0
    apnea_flag: bool = False
    apnea_events: int = 0
    apnea_total_sec: float = 0.0
    apnea_longest_sec: float = 0.0
    sqi: float = 0.0
    sqi_envelope: float = 0.0
    sqi_periodicity: float = 0.0


def _rr_to_hrv(rr_ms: list[float], out: HeartResult) -> tuple[bool, float, float, float]:
    """RR 两段清洗 + HRV 时域指标, 对应 rr_to_hrv()。

    返回 (是否成功, rreg, art, rr_med)。
    """
    n_rr = len(rr_ms)

    # Stage 1: 偏离 11 点局部中位数 > 15% 的丢掉
    filt: list[float] = []
    W = 11
    for i in range(n_rr):
        lo = max(0, i - W // 2)
        hi = min(n_rr - 1, i + W // 2)
        med = _median(rr_ms[lo : hi + 1])
        if med > 1.0:
            dev = abs(rr_ms[i] - med) / med
            if dev < 0.15:
                filt.append(rr_ms[i])

    # Stage 2: |ΔRR| > 150ms 时丢弃距全局中位数较远的那拍
    if len(filt) >= 3:
        global_med = _median(filt)
        filt2: list[float] = []
        drop_next = False
        i = 0
        while i < len(filt):
            if drop_next:
                drop_next = False
                i += 1
                continue
            if i + 1 < len(filt) and abs(filt[i + 1] - filt[i]) > 150.0:
                d1 = abs(filt[i] - global_med)
                d2 = abs(filt[i + 1] - global_med)
                if d1 <= d2:
                    filt2.append(filt[i])
                    drop_next = True
                i += 1
                continue
            filt2.append(filt[i])
            i += 1
        filt = filt2

    m = len(filt)
    if m < 10:
        return False, 0.0, 0.0, 0.0

    mean = sum(filt) / m
    sdnn = math.sqrt(sum((v - mean) ** 2 for v in filt) / (m - 1))
    diffs = [filt[i] - filt[i - 1] for i in range(1, m)]
    rmssd = math.sqrt(sum(d * d for d in diffs) / (m - 1))
    pnn50 = 100.0 * sum(1 for d in diffs if abs(d) > 50.0) / (m - 1)

    rr_med = _median(filt)
    hr_raw = 60000.0 / rr_med
    out.hr_bpm_raw = hr_raw
    out.hr_bpm = max(float(BCG_HR_MIN_BPM), min(float(BCG_HR_MAX_BPM), hr_raw))
    out.mean_rr_ms = mean
    out.sdnn_ms = sdnn
    out.rmssd_ms = rmssd
    out.pnn50_pct = pnn50
    out.n_rr = m
    out.n_rr_raw = n_rr

    rreg = (sum(1 for d in diffs if abs(d) < 50.0) / len(diffs)) if diffs else 0.0
    art = (sum(1 for v in filt if abs(v - rr_med) / rr_med < 0.25) / m) if rr_med > 1.0 else 0.0
    return True, rreg, art, rr_med


def compute_heart(window: list[float]) -> HeartResult:
    """对 200Hz 窗口做一次心率计算, 对应 bcg_compute()。"""
    out = HeartResult()
    N = len(window)
    if N < BCG_MIN_SEC * BCG_FS:
        return out
    out.n_samples_used = N

    mean_sig = sum(window) / N
    s0, s1 = heart_bandpass()

    # 初始不应期: 按先验 75bpm, min_dist = 0.65 × RR_exp
    min_dist = int(BCG_FS * 60.0 / BCG_HR_INIT_BPM * 0.65)
    warmup = 2 * BCG_FS      # 带通瞬态 ~2s 丢弃

    running_max = -1e30
    running_max_idx = -1
    y_prev_at_max = y_peak = y_next_at_max = 0.0
    y_next_captured = False
    prev_y = 0.0
    have_last = False
    last_peak_idx = -1
    last_peak_delta = 0.0
    rr_ms: list[float] = []
    filtered: list[float] = []

    for i in range(N):
        y = s1.tick(s0.tick(window[i] - mean_sig))
        filtered.append(y)
        if i < warmup:
            prev_y = y
            continue

        if y > running_max:
            running_max = y
            running_max_idx = i
            y_prev_at_max = prev_y
            y_peak = y
            y_next_captured = False
        elif not y_next_captured and i == running_max_idx + 1:
            y_next_at_max = y
            y_next_captured = True

        if not have_last:
            if i >= warmup + min_dist and y_next_captured:
                last_peak_idx = running_max_idx
                last_peak_delta = _parabolic_delta(y_prev_at_max, y_peak, y_next_at_max)
                have_last = True
                running_max = -1e30
                y_next_captured = False
        else:
            if ((i - running_max_idx) >= min_dist
                    and (running_max_idx - last_peak_idx) >= min_dist
                    and y_next_captured):
                delta = _parabolic_delta(y_prev_at_max, y_peak, y_next_at_max)
                rr_samp = (running_max_idx - last_peak_idx) + (delta - last_peak_delta)
                ms = rr_samp * 1000.0 / BCG_FS
                if BCG_RR_ACCEPT_MIN_MS <= ms <= BCG_RR_ACCEPT_MAX_MS and len(rr_ms) < BCG_RR_MAX:
                    rr_ms.append(ms)
                last_peak_idx = running_max_idx
                last_peak_delta = delta
                running_max = -1e30
                y_next_captured = False
        prev_y = y

    if len(rr_ms) < 10:
        return out

    ok, rreg, art, rr_med = _rr_to_hrv(rr_ms, out)
    if not ok:
        return out

    aclag = _autocorr_lag(filtered, rr_med, warmup)
    out.sqi = max(0.0, min(100.0, 100.0 * (0.55 * aclag + 0.30 * rreg + 0.15 * art)))
    out.sqi_coverage = aclag
    out.sqi_survival = art
    out.sqi_regularity = rreg
    out.ready = True
    return out


def _autocorr_lag(filtered: list[float], rr_med: float, warmup: int) -> float:
    """SQI 的 aclag 项: 中位 RR 滞后处的归一化自相关峰。

    噪声 / 空床 ≈ 0, 干净准周期心跳 ≈ 0.4-0.7。对应固件里抽取到 100Hz 的单点法。
    """
    lag0 = int(rr_med * BCG_FS / 1000.0 + 0.5)
    lagd = (lag0 + 1) // 2                       # 抽取到 100Hz 后的滞后
    if not (4 < lagd < 250) or warmup + lag0 + 8 >= len(filtered):
        return 0.0

    dl = max(1, lagd // 12)
    lags = (lagd - dl, lagd, lagd + dl)
    ring = [0] * 256
    acc_l = [0.0, 0.0, 0.0]
    acc0 = 0.0
    pos = 0

    for i in range(warmup, len(filtered)):
        if i & 1:                                # 抽取 1/2 → 100Hz
            continue
        yi = max(-32768, min(32767, int(filtered[i])))
        y = float(yi)
        acc0 += y * y
        for k, lag in enumerate(lags):
            if pos >= lag:
                acc_l[k] += y * ring[(pos - lag) & 255]
        ring[pos & 255] = yi
        pos += 1

    if acc0 <= 1e-6:
        return 0.0
    return max(0.0, min(1.0, max(a / acc0 for a in acc_l)))


class HrTracker:
    """跨窗 HR 跟踪: 半/倍频纠正 + 跳变限幅 + 滑动中位数, 对应 bcg_track_hr()。"""

    def __init__(self) -> None:
        self._hist: deque[float] = deque(maxlen=BCG_HR_HIST_N)

    def reset(self) -> None:
        self._hist.clear()

    def track(self, out: HeartResult) -> None:
        hr = out.hr_bpm_raw
        if self._hist:
            base = _median(list(self._hist))
            # 半/倍频纠正需要足够历史(≥3), 避免被早期噪声带偏
            if len(self._hist) >= 3 and base > 1.0:
                r = hr / base
                if BCG_HR_DOUBLE_LO <= r <= BCG_HR_DOUBLE_HI:
                    hr *= 0.5           # 重复计数 → ÷2
                elif BCG_HR_HALF_LO <= r <= BCG_HR_HALF_HI:
                    hr *= 2.0           # 漏拍 → ×2
            hr = max(float(BCG_HR_MIN_BPM), min(float(BCG_HR_MAX_BPM), hr))
            step = hr - base
            if step > BCG_HR_MAX_STEP_BPM:
                hr = base + BCG_HR_MAX_STEP_BPM
            elif step < -BCG_HR_MAX_STEP_BPM:
                hr = base - BCG_HR_MAX_STEP_BPM
        else:
            hr = max(float(BCG_HR_MIN_BPM), min(float(BCG_HR_MAX_BPM), hr))

        self._hist.append(hr)
        smoothed = _median(list(self._hist))
        out.hr_bpm = smoothed
        out.mean_rr_ms = 60000.0 / smoothed


def compute_resp(window: list[float]) -> RespResult:
    """对 25Hz 窗口做一次呼吸计算 + 暂停检测, 对应 resp_core()。"""
    out = RespResult()
    n = len(window)
    if n < BCG_RESP_MIN_WIN_SEC * BCG_RESP_FS:
        return out
    out.n_samples_used = n

    mean_sig = sum(window) / n
    s0, s1 = resp_bandpass()

    min_dist = int(BCG_RESP_FS * 60.0 / BCG_RESP_MAX_BPM)     # 50 = 2s @25Hz
    warmup = 8 * BCG_RESP_FS                                   # 0.1Hz 带通瞬态 8s
    per_min = 60000.0 / BCG_RESP_MAX_BPM                       # 2000ms
    per_max = 60000.0 / BCG_RESP_MIN_BPM                       # 10000ms

    # ---- Pass 1: 带通 + 峰检 + 整流包络统计 ----
    periods: list[float] = []
    n_raw = 0
    running_max = -1e30
    rmax_idx = -1
    last_peak = -1
    env_sum = env_sq = 0.0
    env_n = 0

    for i in range(n):
        y = s1.tick(s0.tick(window[i] - mean_sig))
        if i < warmup:
            continue
        ay = abs(y)
        env_sum += ay
        env_sq += ay * ay
        env_n += 1
        if y > running_max:
            running_max = y
            rmax_idx = i
        if rmax_idx >= 0 and (i - rmax_idx) >= min_dist:
            if last_peak < 0:
                last_peak = rmax_idx
            elif (rmax_idx - last_peak) >= min_dist:
                n_raw += 1
                ms = (rmax_idx - last_peak) * 1000.0 / BCG_RESP_FS
                if per_min <= ms <= per_max and len(periods) < BCG_RESP_BR_MAX:
                    periods.append(ms)
                last_peak = rmax_idx
            running_max = -1e30
            rmax_idx = -1

    if len(periods) < 3 or env_n == 0:
        return out

    # ---- 周期中位数清洗 ±30% ----
    gmed = _median(periods)
    clean = [p for p in periods if abs(p - gmed) / gmed <= 0.30]
    if len(clean) < 2:
        return out
    cmed = _median(clean)
    rr_bpm = max(float(BCG_RESP_MIN_BPM), min(float(BCG_RESP_MAX_BPM), 60000.0 / cmed))

    # ---- 包络稳定度 + 周期规律 ----
    env_mean = env_sum / env_n
    env_var = max(0.0, env_sq / env_n - env_mean * env_mean)
    env_std = math.sqrt(env_var)
    env_score = max(0.0, min(1.0, 1.0 - env_std / env_mean)) if env_mean > 1e-6 else 0.0

    per_score = 1.0
    dt = [abs(clean[i] - clean[i - 1]) for i in range(1, len(clean))]
    if dt:
        dmed = _median(dt)
        per_score = max(0.0, min(1.0, 1.0 - dmed / cmed)) if cmed > 1e-6 else 0.0

    # ---- Pass 2: 2s EMA 包络 → 呼吸暂停 (连续 < 0.3×均值 且 ≥10s) ----
    s0.reset()
    s1.reset()
    thr = 0.30 * env_mean
    alpha = 1.0 / (2.0 * BCG_RESP_FS)
    apnea_n = BCG_RESP_APNEA_SEC * BCG_RESP_FS
    env = env_mean
    low_run = 0
    events = 0
    total_s = longest_s = 0.0

    for i in range(n):
        y = s1.tick(s0.tick(window[i] - mean_sig))
        if i < warmup:
            continue
        env += alpha * (abs(y) - env)
        if env < thr:
            low_run += 1
        else:
            if low_run >= apnea_n:
                sec = low_run / BCG_RESP_FS
                events += 1
                total_s += sec
                longest_s = max(longest_s, sec)
            low_run = 0
    if low_run >= apnea_n:
        sec = low_run / BCG_RESP_FS
        events += 1
        total_s += sec
        longest_s = max(longest_s, sec)
        out.apnea_flag = True

    out.rr_bpm = rr_bpm
    out.mean_period_ms = cmed
    out.n_breaths = len(clean)
    out.n_breaths_raw = n_raw
    out.apnea_events = events
    out.apnea_total_sec = total_s
    out.apnea_longest_sec = longest_s
    out.sqi = 100.0 * (0.5 * env_score + 0.5 * per_score)
    out.sqi_envelope = env_score
    out.sqi_periodicity = per_score
    out.ready = True
    return out


# ---- 波形显示 ----
# 窗口要短才看得出在动: 推送 20Hz 时, 心跳每次前进 5 点(占 1.25% 宽度), 呼吸 1.25 点。
# 窗口再长的话每次只挪不到 1%, 视觉上就像静止。
HEART_WAVE_POINTS = 400
HEART_WAVE_DECIM = 2      # 200Hz ÷2 = 100Hz → 400 点 = 4 秒
RESP_WAVE_POINTS = 200    # 25Hz → 200 点 = 8 秒
RESP_DECIM = BCG_FS // BCG_RESP_FS   # 8:1, 与固件 resp_get_batch 一致


class VitalsEngine:
    """单路 200Hz 原始 BCG 信号 → 心率 + 呼吸。

    下位机 ``D`` 协议每帧给两路 8 位值, 只用其中动态范围大的那一路(原始 BCG),
    心跳和呼吸都是从**同一路信号**用不同带通分出来的 —— 这也是 C65 固件的做法。

    * 心跳: 200Hz 原速 → 0.6-2.5Hz 带通 → 峰检 → RR → HR
    * 呼吸: 8:1 抽取到 25Hz → 0.1-0.5Hz 带通 → 峰检 → 周期 → BR

    页面上画的波形和检测器用的是**同一组带通系数**(各自独立的滤波器状态),
    所见即算法所见。
    """

    def __init__(self) -> None:
        self._tracker = HrTracker()
        self.reset()

    def reset(self) -> None:
        self.heart_window: deque[float] = deque(maxlen=BCG_WIN_N)
        self.resp_window: deque[float] = deque(maxlen=BCG_RESP_WIN_N)
        self.heart_wave: deque[float] = deque([0.0] * HEART_WAVE_POINTS,
                                              maxlen=HEART_WAVE_POINTS)
        self.resp_wave: deque[float] = deque([0.0] * RESP_WAVE_POINTS,
                                             maxlen=RESP_WAVE_POINTS)
        self._heart_wave_filter = heart_bandpass()
        self._resp_wave_filter = resp_bandpass()
        self._heart_wave_phase = 0
        self._resp_acc = 0.0
        self._resp_n = 0
        self._tracker.reset()

    def push(self, sample: float) -> None:
        """喂一个 200Hz 原始采样。"""
        self.heart_window.append(sample)

        h0, h1 = self._heart_wave_filter
        filtered = h1.tick(h0.tick(sample))
        self._heart_wave_phase += 1
        if self._heart_wave_phase >= HEART_WAVE_DECIM:
            self._heart_wave_phase = 0
            self.heart_wave.append(filtered)

        # 8:1 抗混叠均值抽取 → 25Hz 呼吸链路
        self._resp_acc += sample
        self._resp_n += 1
        if self._resp_n >= RESP_DECIM:
            value = self._resp_acc / self._resp_n
            self._resp_acc = 0.0
            self._resp_n = 0
            self.resp_window.append(value)
            r0, r1 = self._resp_wave_filter
            self.resp_wave.append(r1.tick(r0.tick(value)))

    @property
    def seconds(self) -> float:
        return len(self.heart_window) / BCG_FS

    def compute(self) -> tuple[HeartResult, RespResult]:
        """跑一次心率 + 呼吸计算。数据不足时返回 ready=False 的结果。"""
        heart = compute_heart(list(self.heart_window))
        if heart.ready:
            self._tracker.track(heart)
        resp = compute_resp(list(self.resp_window))
        return heart, resp
