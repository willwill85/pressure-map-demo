"""逐点卡尔曼滤波器, 移植自 SleepMonitor/MonitorService.cs 的 kalman 类。

压力垫每个测点各有一个独立实例, 参数 (A=1, C=1, Q=0.4, R=3.4) 与 C# 一致。
"""

from __future__ import annotations


class Kalman:
    __slots__ = ("_a", "_a2", "_c", "_c2", "_q", "_r", "_k", "_p", "_x")

    def __init__(self, a: float = 1.0, c: float = 1.0, q: float = 0.4, r: float = 3.4) -> None:
        self._a = a
        self._a2 = a * a
        self._c = c
        self._c2 = c * c
        self._q = q
        self._r = r
        self._x = 0.0
        self._p = q
        self._k = 1.0

    def calc(self, y: float) -> float:
        self._x = self._a * self._x                                   # 预测状态
        self._p = self._a2 * self._p + self._q                        # 预测协方差
        self._k = self._p * self._c / (self._c2 * self._p + self._r)  # 卡尔曼增益
        self._x = self._x + self._k * (y - self._c * self._x)         # 更新状态
        self._p = (1 - self._k * self._c) * self._p                   # 更新协方差
        return self._c * self._x
