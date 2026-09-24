"""压力热图处理, 移植自 SleepMonitor/MonitorService.cs 的 OnFrameReady / GaussianFilter。

    串口 514 字节 → 16×32 逐点卡尔曼 → ×3 放大成 48×96 → 5×5 高斯模糊 → ÷12 归一化
"""

from __future__ import annotations

import numpy as np

from .kalman import Kalman

MAP_WIDTH = 16
MAP_HEIGHT = 32
MULT = 3                                # 放大倍数, 对应 C# 的 MULT
GRID_W = MAP_WIDTH * MULT               # 48
GRID_H = MAP_HEIGHT * MULT              # 96
FRAME_SIZE = MAP_WIDTH * MAP_HEIGHT + 2  # 514


def gaussian_kernel(size: int = 5, weight: float = 1.0) -> np.ndarray:
    """对应 C# GaussianFilter 的核构造 —— 注意分母是 3·weight², 不是常见的 2σ²。"""
    radius = size // 2
    coords = np.arange(-radius, radius + 1, dtype=np.float64)
    xx, yy = np.meshgrid(coords, coords, indexing="ij")
    kernel = np.exp(-(xx * xx + yy * yy) / (3.0 * weight * weight))
    return kernel / kernel.sum()


def gaussian_filter(matrix: np.ndarray, size: int = 5, weight: float = 1.0) -> np.ndarray:
    """零填充卷积。边界外按 0 计入且**不重新归一化** —— 与 C# 一致。"""
    kernel = gaussian_kernel(size, weight)
    radius = size // 2
    padded = np.pad(matrix.astype(np.float64), radius, mode="constant", constant_values=0.0)
    out = np.zeros(matrix.shape, dtype=np.float64)
    for ki in range(size):
        for kj in range(size):
            out += padded[ki : ki + matrix.shape[0], kj : kj + matrix.shape[1]] * kernel[ki, kj]
    return out


# ---- 在离床判定 ----
# 单点超过 CELL_THRESHOLD 记 1 分, 全垫累计超过 COUNT_THRESHOLD 认为有人。
# 空垫时受力点很少, 躺人后身体轮廓下的一片测点都会亮起来, 两个阈值都可以在
# 命令行调 (--on-bed-cell / --on-bed-count), 页面上会实时显示当前计数便于标定。
DEFAULT_CELL_THRESHOLD = 30
DEFAULT_COUNT_THRESHOLD = 40

# 迟滞: 进入需连续 N 帧成立, 离开需连续 M 帧不成立, 避免翻身瞬间闪断
ENTER_FRAMES = 3
LEAVE_FRAMES = 15


class PressureMap:
    """把串口帧转成页面 Three.js 要的 48×96 浮点数组, 并做在离床判定。"""

    def __init__(self, cell_threshold: int = DEFAULT_CELL_THRESHOLD,
                 count_threshold: int = DEFAULT_COUNT_THRESHOLD) -> None:
        self.cell_threshold = cell_threshold
        self.count_threshold = count_threshold
        self._kalman = [[Kalman(1.0, 1.0, 0.4, 3.4) for _ in range(MAP_HEIGHT)]
                        for _ in range(MAP_WIDTH)]
        self.press_map = np.zeros(GRID_W * GRID_H, dtype=np.float32)
        self.active_cells = 0        # 当前超阈值的测点数
        self.occupied = False        # 迟滞后的在床结论
        self._enter_run = 0
        self._leave_run = 0

    def reset(self) -> None:
        self.__init__(self.cell_threshold, self.count_threshold)

    def _update_occupancy(self, pressure_map: np.ndarray) -> None:
        """单点过阈值计数 → 总数过阈值判有人, 带进入/离开迟滞。"""
        self.active_cells = int((pressure_map > self.cell_threshold).sum())
        above = self.active_cells >= self.count_threshold

        if above:
            self._leave_run = 0
            self._enter_run += 1
            if not self.occupied and self._enter_run >= ENTER_FRAMES:
                self.occupied = True
        else:
            self._enter_run = 0
            self._leave_run += 1
            if self.occupied and self._leave_run >= LEAVE_FRAMES:
                self.occupied = False

    def process_frame(self, frame: bytes) -> np.ndarray:
        """处理一帧(514 字节, 前 2 字节是帧头), 返回归一化后的压力图。"""
        raw = np.frombuffer(bytes(frame), dtype=np.uint8)
        payload = raw[2 : 2 + MAP_WIDTH * MAP_HEIGHT].astype(np.float64)
        if payload.size < MAP_WIDTH * MAP_HEIGHT:
            payload = np.pad(payload, (0, MAP_WIDTH * MAP_HEIGHT - payload.size))

        # frame[j * MAP_WIDTH + i + 2] → grid[i, j]
        grid = payload.reshape(MAP_HEIGHT, MAP_WIDTH).T

        filtered = np.empty((MAP_WIDTH, MAP_HEIGHT), dtype=np.float64)
        for i in range(MAP_WIDTH):
            row = self._kalman[i]
            for j in range(MAP_HEIGHT):
                filtered[i, j] = row[j].calc(grid[i, j]) * 1.5

        # map[i, MAP_HEIGHT-1-j] = (int)(...)   C# 的 (int) 是向零截断
        pressure_map = np.trunc(filtered).astype(np.int32)[:, ::-1]
        self._update_occupancy(pressure_map)

        # smallmap[i*3+m, j*3+n] = map[MAP_WIDTH-1-i, j]   (j 不翻转)
        small = np.repeat(np.repeat(pressure_map[::-1, :], MULT, axis=0), MULT, axis=1)
        smooth = gaussian_filter(small, size=5, weight=1.0)

        # press_map[index++] = smooth[j, (GRID_H-1)-i] / 12.0, 行优先 96 行 × 48 列
        self.press_map = (smooth[:, ::-1].T / 12.0).astype(np.float32).reshape(-1)
        return self.press_map
